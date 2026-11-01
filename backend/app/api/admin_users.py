"""User management API router (admin-console, task 8).

Admin-only CRUD over user accounts (prefix ``/api/admin/users``):

- ``GET    /``              -- list accounts (no password hashes).
- ``POST   /``              -- create an account (409 on duplicate username).
- ``PUT    /{id}``          -- change ``is_admin`` / ``disabled``.
- ``POST   /{id}/password`` -- reset a user's password.
- ``DELETE /{id}``          -- delete an account (cascades its meetings).

Invariants enforced:
- **At-least-one-admin** (Property 7): no demote/disable/delete may leave zero
  enabled administrators -> 409.
- **Session revocation** (Property 8): disabling, deleting, resetting the
  password of, or revoking admin from a user invalidates that user's sessions.

Every write records an Audit_Log_Entry (Req 10.1). All routes are gated by
:data:`AdminUser`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import AdminUser
from app.audit import record_audit
from app.config import Settings, get_settings
from app.db import get_session
from app.models import User
from app.security.passwords import hash_password
from app.security.sessions import invalidate_user_sessions

router = APIRouter(prefix="/api/admin/users", tags=["admin"])


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str


def _serialize(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "disabled": user.disabled,
        "created_at": user.created_at.isoformat(),
    }


def _enabled_admin_count(db: OrmSession) -> int:
    """Number of enabled administrators currently visible in the transaction."""
    return int(
        db.execute(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.disabled.is_(False))
        ).scalar_one()
    )


def _credential_ok(value: str, max_len: int) -> bool:
    return 0 < len(value) <= max_len


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": detail})


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "user not found"})


def _last_admin_conflict() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "operation would remove the last enabled administrator"},
    )


@router.get("")
def list_users(
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """List all accounts (Req 7.1); never includes password hashes."""
    users = db.execute(select(User).order_by(User.created_at.asc())).scalars().all()
    return JSONResponse(status_code=200, content=[_serialize(u) for u in users])


@router.post("")
def create_user(
    body: CreateUserRequest,
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Create an account with a salted password hash (Req 7.2/7.3)."""
    max_len = settings.max_credential_length
    if not _credential_ok(body.username, max_len) or not _credential_ok(
        body.password, max_len
    ):
        return _bad_request(
            "username and password must be non-empty and at most "
            f"{max_len} characters"
        )

    existing = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if existing is not None:
        return JSONResponse(
            status_code=409, content={"detail": "username already exists"}
        )

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
    )
    db.add(user)
    db.flush()
    record_audit(
        db,
        actor=admin,
        action="user.create",
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "is_admin": user.is_admin},
    )
    db.commit()
    return JSONResponse(status_code=201, content=_serialize(user))


@router.put("/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Change ``is_admin`` / ``disabled``; enforce the at-least-one-admin rule.

    A change that reduces privilege (disable, or revoke admin) invalidates the
    target's active sessions (Property 8). If the change would leave zero enabled
    administrators it is rejected with 409 and rolled back (Property 7).
    """
    user = db.get(User, user_id)
    if user is None:
        return _not_found()

    changed: dict[str, Any] = {}
    reduces_privilege = False
    if body.is_admin is not None and body.is_admin != user.is_admin:
        if body.is_admin is False:
            reduces_privilege = True
        user.is_admin = body.is_admin
        changed["is_admin"] = body.is_admin
    if body.disabled is not None and body.disabled != user.disabled:
        if body.disabled is True:
            reduces_privilege = True
        user.disabled = body.disabled
        changed["disabled"] = body.disabled

    if not changed:
        return JSONResponse(status_code=200, content=_serialize(user))

    db.flush()
    # At-least-one-admin invariant: re-check AFTER applying the change.
    if _enabled_admin_count(db) == 0:
        db.rollback()
        return _last_admin_conflict()

    if reduces_privilege:
        invalidate_user_sessions(db, user.id)

    record_audit(
        db,
        actor=admin,
        action="user.update",
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "changed": changed},
    )
    db.commit()
    return JSONResponse(status_code=200, content=_serialize(user))


@router.post("/{user_id}/password")
def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Admin password reset; replaces the hash and invalidates the target's sessions."""
    user = db.get(User, user_id)
    if user is None:
        return _not_found()
    if not _credential_ok(body.password, settings.max_credential_length):
        return _bad_request(
            "password must be non-empty and at most "
            f"{settings.max_credential_length} characters"
        )

    user.password_hash = hash_password(body.password)
    db.flush()
    invalidate_user_sessions(db, user.id)
    record_audit(
        db,
        actor=admin,
        action="user.password_reset",
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
    )
    db.commit()
    return JSONResponse(
        status_code=200, content={"id": user.id, "password_reset": True}
    )


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Delete an account (cascading its meetings) and invalidate its sessions.

    Rejected with 409 if it would remove the last enabled administrator
    (Property 7). The cascade removes the user's Meeting_Records and their data
    (meeting-minutes-service Req 9.4).
    """
    user = db.get(User, user_id)
    if user is None:
        return _not_found()

    username = user.username
    # Record the audit and invalidate sessions BEFORE the delete, so that if the
    # acting admin deletes their own account the audit row's actor FK is set null
    # by the cascade (rather than violating the FK on a post-delete insert). If
    # the invariant check below fails, the whole transaction -- audit included --
    # is rolled back (Property 9).
    invalidate_user_sessions(db, user.id)
    record_audit(
        db,
        actor=admin,
        action="user.delete",
        target_type="user",
        target_id=user_id,
        detail={"username": username},
    )
    db.delete(user)
    db.flush()
    if _enabled_admin_count(db) == 0:
        db.rollback()
        return _last_admin_conflict()

    db.commit()
    return JSONResponse(status_code=200, content={"id": user_id, "deleted": True})
