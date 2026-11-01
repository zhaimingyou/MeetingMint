"""Reusable API dependencies (task 3.5).

This module provides the single authentication dependency that guards every
**non-public** Backend_API endpoint. Per the design's Backend_API surface and
Requirement 1.1:

    All endpoints except ``POST /api/auth/login`` and ``GET /api/health``
    require a valid ``Authorization: Bearer <token>``; otherwise the request is
    rejected with **401 and no content** (no meeting content is returned).

Design Property 3 ("Protected endpoints reject unauthenticated access") states
that for any non-public endpoint and any request bearing an absent, malformed,
expired, or invalidated token, the API responds 401 and returns no meeting
content. :func:`get_current_user` is the chokepoint that makes that hold for
every protected route uniformly: a route simply declares the :data:`CurrentUser`
dependency and is thereby guarded, with the authenticated :class:`User` injected
on success.

No-content 401 approach
-----------------------
A failed authentication raises :class:`AuthenticationError` (a plain exception,
**not** :class:`fastapi.HTTPException`). ``app.main.create_app`` registers
:func:`authentication_exception_handler` for it, which returns a bare
:class:`~fastapi.Response` with status ``401`` and an empty body plus a
``WWW-Authenticate: Bearer`` header. Two reasons for the custom exception rather
than ``HTTPException``:

- ``HTTPException`` is rendered by FastAPI's default handler as a JSON body
  ``{"detail": ...}``; the design calls for **no content**, so we return a truly
  empty body instead.
- Overriding the *global* ``HTTPException`` handler to strip bodies would affect
  unrelated responses (404s, 415s, ...). A dedicated exception keeps the
  no-content behavior scoped to authentication failures only.

Token extraction (:func:`extract_bearer_token`) accepts the ``Bearer`` scheme
case-insensitively (RFC 6750) and treats an absent, malformed, or empty-token
header as "no token". The auth router (:mod:`app.api.auth`) reuses the same
helper for its logout handler so the two stay consistent.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as OrmSession

from app.db import get_session
from app.models import User
from app.security.sessions import validate_session

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "extract_bearer_token",
    "get_current_user",
    "CurrentUser",
    "require_admin",
    "AdminUser",
    "authentication_exception_handler",
    "authorization_exception_handler",
]


class AuthenticationError(Exception):
    """Raised by :func:`get_current_user` when authentication fails.

    Carries no payload: its only job is to signal that the request lacks a
    valid bearer token so the registered handler can emit a no-content 401.
    Distinct from :class:`fastapi.HTTPException` precisely so that the
    no-content rendering is scoped to auth failures and does not affect other
    error responses.
    """


class AuthorizationError(Exception):
    """Raised by :func:`require_admin` when an authenticated user lacks admin.

    Distinct from :class:`AuthenticationError` so the two map to different
    statuses: an unauthenticated request is a no-content 401 (handled by
    :func:`authentication_exception_handler`), while an authenticated
    non-administrator is a 403 with a short ``{"detail": ...}`` body (handled by
    :func:`authorization_exception_handler`). The admin-console feature uses this
    to gate every ``/api/admin/*`` operation.
    """


def extract_bearer_token(authorization: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` when the header is absent, not a two-part
    ``"<scheme> <token>"`` value, uses a scheme other than ``Bearer``, or
    carries an empty token. The ``Bearer`` scheme is matched case-insensitively
    per RFC 6750 (so ``bearer`` / ``BEARER`` are accepted).

    Examples of inputs that yield ``None`` (treated as unauthenticated):
    ``None``, ``""``, ``"Bearer"`` (no token), ``"Bearer "`` (empty token),
    ``"Token abc"`` (wrong scheme).
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def get_current_user(
    db: Annotated[OrmSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Resolve the authenticated :class:`User` or reject with a no-content 401.

    Extracts the bearer token from the ``Authorization`` header and validates it
    against the request-scoped database via
    :func:`app.security.sessions.validate_session`. The token is accepted only
    when the session exists, is not invalidated, and has not expired. In every
    other case -- absent/malformed/empty header, or unknown/expired/invalidated
    token -- this raises :class:`AuthenticationError`, which the registered
    handler turns into a 401 with no body (Req 1.1, Property 3).

    Use it on any protected route via the :data:`CurrentUser` alias so the route
    receives the current user and is guarded uniformly.
    """
    token = extract_bearer_token(authorization)
    if token is None:
        raise AuthenticationError
    user = validate_session(db, token)
    if user is None:
        raise AuthenticationError
    return user


# Convenience alias for protected routes: ``user: CurrentUser`` both guards the
# endpoint (401 no-content on failure) and injects the authenticated user.
CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    """Resolve the authenticated :class:`User` and require the admin role.

    Depends on :func:`get_current_user`, so an absent/invalid token is rejected
    first with a no-content 401 (Req 1.1). For an authenticated user that is
    **not** an Administrator, raises :class:`AuthorizationError`, which the
    registered handler turns into a 403 (Req 1.4). Use it on every Admin_Console
    route via the :data:`AdminUser` alias.
    """
    if not user.is_admin:
        raise AuthorizationError
    return user


# Convenience alias for Admin_Console routes: ``user: AdminUser`` guards the
# endpoint (401 no-content when unauthenticated, 403 when authenticated but not
# an administrator) and injects the acting administrator.
AdminUser = Annotated[User, Depends(require_admin)]


def authentication_exception_handler(
    request: Request, exc: AuthenticationError
) -> Response:
    """Render :class:`AuthenticationError` as a no-content 401.

    Returns an empty-body ``401 Unauthorized`` with a ``WWW-Authenticate:
    Bearer`` challenge header. Registered for the whole app in
    ``app.main.create_app`` so it applies to every protected router. The empty
    body satisfies the design's "401 with no content" / Req 1.1 "SHALL NOT
    return any meeting content".
    """
    return Response(
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def authorization_exception_handler(
    request: Request, exc: AuthorizationError
) -> Response:
    """Render :class:`AuthorizationError` as a 403 with a short message.

    Registered app-wide in ``app.main.create_app`` so every Admin_Console route
    guarded by :data:`AdminUser` rejects an authenticated non-administrator with
    a 403 and discloses no Admin_Console data (Req 1.4).
    """
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": "administrator privilege required"},
    )
