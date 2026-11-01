"""Authentication API router (task 3.4).

Implements the two public-facing auth endpoints from the design's Backend_API
surface and Requirements 1.2 / 1.3 / 1.4 / 1.5:

- ``POST /api/auth/login`` -- authenticate a username/password pair. On success
  it establishes a server-side session and returns the opaque bearer token
  (valid for ``session_ttl_minutes``, default 60). The validation order is
  deliberate and **400 takes precedence over 401**:

    1. If either credential field is empty/missing **or** longer than
       ``max_credential_length`` (256), reject with **400** and create no
       session. This guards against malformed/out-of-bounds input *before* any
       credential lookup (Req 1.4).
    2. Otherwise look the user up and verify the password. On a match, purge
       stale sessions (design "purge on login"), issue a session, commit, and
       return **200** + token. On any mismatch (unknown user or wrong password)
       reject with **401**, creating no session and returning no token
       (Req 1.2 / 1.3).

  To avoid trivially leaking (via response timing) whether a username exists,
  the unknown-user branch still runs a bcrypt verification against a fixed dummy
  hash so both the unknown-user and wrong-password paths perform comparable
  work. This is best-effort, not a hard constant-time guarantee.

- ``POST /api/auth/logout`` -- invalidate the caller's session token, read
  directly from the ``Authorization: Bearer <token>`` header. Idempotent:
  unknown/garbage/missing tokens still return success (Req 1.5). The generic
  auth dependency that guards *protected* routes is task 3.5; this endpoint
  reads the header itself so it does not depend on 3.5.

Transaction discipline: the route obtains a request-scoped session via
``Depends(app.db.get_session)`` and commits explicitly, honoring the
caller-owns-commit contract of :mod:`app.security.sessions`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings, get_settings
from app.config_effective import effective_settings_for
from app.db import get_session
from app.api.deps import CurrentUser, extract_bearer_token
from app.models import User
from app.security.passwords import hash_password, verify_password
from app.security.sessions import (
    invalidate_session,
    invalidate_user_sessions,
    issue_session,
    purge_sessions,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# A fixed bcrypt hash used only to spend roughly the same time verifying a
# password when the username is unknown, so an attacker cannot trivially
# distinguish "no such user" from "wrong password" by response timing. The
# plaintext behind it is irrelevant -- it must never match a real submission.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-equalization")


class LoginRequest(BaseModel):
    """Login request body.

    Both fields are required strings. Emptiness and the 256-char upper bound are
    validated inside the handler (not here) so a violation maps to a 400 with a
    clear message rather than a 422 from request-model validation -- matching
    Req 1.4's "HTTP 400" contract.
    """

    username: str
    password: str


class LoginResponse(BaseModel):
    """Successful login response carrying the opaque bearer token."""

    token: str
    token_type: str = Field(default="bearer")
    # Whether the authenticated account is an Administrator, so the Web_Frontend
    # can decide whether to present the Admin_Console entry points (Req 1.6).
    is_admin: bool = False


class MessageResponse(BaseModel):
    """Generic ``{"detail": ...}`` style message body for non-200 paths."""

    detail: str


def _field_out_of_bounds(value: str, max_length: int) -> bool:
    """True if ``value`` is empty or exceeds ``max_length`` characters (Req 1.4)."""
    return len(value) == 0 or len(value) > max_length


@router.post(
    "/login",
    responses={
        status.HTTP_200_OK: {"model": LoginResponse},
        status.HTTP_400_BAD_REQUEST: {"model": MessageResponse},
        status.HTTP_401_UNAUTHORIZED: {"model": MessageResponse},
    },
)
def login(
    body: LoginRequest,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Authenticate and, on success, return a 60-minute session token."""
    max_len = settings.max_credential_length

    # (1) Out-of-bounds / empty fields -> 400, before any credential lookup.
    #     This precedence means a malformed request is never answered with 401.
    if _field_out_of_bounds(body.username, max_len) or _field_out_of_bounds(
        body.password, max_len
    ):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": (
                    "username and password must be non-empty and at most "
                    f"{max_len} characters"
                )
            },
        )

    # (2) Look up the user and verify the password.
    user = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()

    if user is None:
        # Spend comparable time even when the user does not exist so the
        # unknown-user and wrong-password paths are not trivially distinguishable
        # by timing. Result is intentionally ignored.
        verify_password(body.password, _DUMMY_PASSWORD_HASH)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid credentials"},
        )

    if not verify_password(body.password, user.password_hash):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid credentials"},
        )

    # A disabled account cannot authenticate (admin-console Req 7.5/7.9). Checked
    # AFTER password verification so a disabled account is not distinguishable
    # from a wrong password by response timing, and answered with the same
    # generic 401.
    if user.disabled:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid credentials"},
        )

    # (3) Valid credentials: purge stale rows then issue a fresh session, and
    #     commit the whole unit of work (caller-owns-commit contract).
    purge_sessions(db)
    # Pass the TTL from the injected settings so the session lifetime honors the
    # request-scoped configuration (and any test override) rather than a
    # separately-resolved process-wide settings instance.
    session = issue_session(db, user.id, ttl_minutes=settings.session_ttl_minutes)
    db.commit()

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=LoginResponse(token=session.token, is_admin=user.is_admin).model_dump(),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    db: Annotated[OrmSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Invalidate the caller's session token. Idempotent.

    Reads the bearer token straight from the ``Authorization`` header using the
    shared :func:`app.api.deps.extract_bearer_token` helper (so logout and the
    protected-route auth dependency parse the header identically). An absent,
    malformed, unknown, or already-invalidated token is a no-op that still
    returns 204 -- logging out never fails (Req 1.5).
    """
    token = extract_bearer_token(authorization)
    if token is not None:
        invalidate_session(db, token)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
def me(user: CurrentUser) -> Response:
    """Return the authenticated user's identity and role (admin-console).

    Guarded by :data:`CurrentUser`, so an absent/invalid token is a no-content
    401. Returns ``{username, is_admin}`` so the Web_Frontend can gate the
    Admin_Console entry points (Req 1.6) and the rest of the app can show the
    current user.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"username": user.username, "is_admin": user.is_admin},
    )


class PasswordChangeRequest(BaseModel):
    """Body of ``POST /api/auth/password`` (self-service change, Req 8)."""

    current_password: str
    new_password: str


@router.post("/password")
def change_password(
    body: PasswordChangeRequest,
    user: CurrentUser,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Change the caller's own password (admin-console Req 8).

    Verifies the current password, validates the new one against the credential
    constraints, replaces the stored hash with a fresh salted hash, and
    invalidates the user's **other** sessions while keeping the requesting one
    valid (Req 8.4). Wrong current password -> 401; weak/empty new password ->
    400; the stored hash is unchanged on either rejection.
    """
    if not verify_password(body.current_password, user.password_hash):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "current password is incorrect"},
        )

    max_len = settings.max_credential_length
    if len(body.new_password) == 0 or len(body.new_password) > max_len:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": (
                    "new password must be non-empty and at most "
                    f"{max_len} characters"
                )
            },
        )

    user.password_hash = hash_password(body.new_password)
    db.flush()
    # Sign out other devices but keep the requesting session valid (Req 8.4).
    token = extract_bearer_token(authorization)
    invalidate_user_sessions(db, user.id, except_token=token)
    db.commit()
    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"changed": True}
    )


class RegisterRequest(BaseModel):
    """Self-service registration body (POST /api/auth/register).

    Both fields are required strings; emptiness and the 256-char upper bound are
    validated inside the handler (not here) so a violation maps to a 400 with a
    clear message rather than a 422 -- mirroring :class:`LoginRequest`.
    """

    username: str
    password: str


def _serialize_public_user(user: User) -> dict[str, object]:
    """Return the non-secret projection of a newly registered user.

    Mirrors ``admin_users._serialize`` but lives here so the auth router has no
    cross-module dependency for a trivial dict shape. Never includes the password
    hash.
    """
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "disabled": user.disabled,
        "created_at": user.created_at.isoformat(),
    }


@router.post(
    "/register",
    responses={
        status.HTTP_201_CREATED: {"model": MessageResponse},
        status.HTTP_400_BAD_REQUEST: {"model": MessageResponse},
        status.HTTP_403_FORBIDDEN: {"model": MessageResponse},
        status.HTTP_409_CONFLICT: {"model": MessageResponse},
    },
)
def register(
    body: RegisterRequest,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Self-service account registration.

    A public endpoint (no bearer token required). It is gated by the runtime
    configuration flag ``allow_public_registration``: when closed (the default)
    it rejects with **403**; when open it creates a non-admin, enabled account
    with a freshly salted bcrypt hash.

    Validation order (deliberate, mirroring ``login``):

    1. The registration gate is checked first -- a closed instance never reveals
       whether a username is taken (403 before any credential lookup).
    2. Empty or over-``max_credential_length`` fields -> **400**, before any
       duplicate check.
    3. An existing username -> **409**.

    Newly registered accounts are always ``is_admin=False`` and
    ``disabled=False`` (immediately usable, no approval step); the password is
    hashed before storage and never persisted in plaintext. Registration does
    **not** establish a session -- the client is expected to call ``/login``
    afterwards (separation of registration and authentication).
    """
    # (1) Gate: read the effective configuration (env + runtime overrides) so an
    #     administrator can flip the switch without a restart.
    effective = effective_settings_for(settings, db)
    if not effective.allow_public_registration:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "registration is not enabled"},
        )

    # (2) Out-of-bounds / empty fields -> 400.
    max_len = effective.max_credential_length
    if _field_out_of_bounds(body.username, max_len) or _field_out_of_bounds(
        body.password, max_len
    ):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": (
                    "username and password must be non-empty and at most "
                    f"{max_len} characters"
                )
            },
        )

    # (3) Duplicate username -> 409.
    existing = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if existing is not None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "username already exists"},
        )

    # Create a non-admin, enabled account with a salted hash.
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        is_admin=False,
        disabled=False,
    )
    db.add(user)
    db.flush()
    db.commit()
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_serialize_public_user(user),
    )
