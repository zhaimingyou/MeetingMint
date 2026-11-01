"""Opaque server-side session management (task 3.2).

This module implements the authoritative, server-side session lifecycle from
Requirements 1.2 / 1.5 / 1.6 and design Property 2 ("Session validity follows
issuance, expiry, and invalidation"). Sessions live in the ``sessions`` table
(``app.models.Session``) and are the single source of truth for whether a
bearer token is currently authentic -- unlike stateless tokens, logout and
expiry are enforced by reading server state on every validation.

A session token is **valid** iff all three hold:

- it **exists** in the table, and
- it is **not invalidated** (logout has not been called), and
- the current instant is **strictly before** ``expires_at`` (``now < expires_at``).

``expires_at`` is fixed at issuance to ``issued_at + session_ttl_minutes``
(default 60 minutes, from :class:`app.config.Settings`). The boundary is exact:
a session is valid for ``now`` in ``[issued_at, expires_at)`` and invalid at
``now == expires_at`` and beyond.

Tokens are opaque and unguessable: each is generated with
:func:`secrets.token_urlsafe` (cryptographically secure, URL-safe base64,
256 bits of entropy by default here), carrying no user data.

Transaction / commit contract
-----------------------------
**The caller owns the transaction; these functions never commit.** Every
function operates on a SQLAlchemy ``Session`` supplied by the caller and uses
``add`` / ``flush`` / ``execute`` so the changes are visible *within* the
caller's transaction, but it is the caller's responsibility to ``commit()``
(or ``rollback()``). This matches the rest of the codebase:

- HTTP routes (tasks 3.4/3.5) take a request-scoped session via
  ``Depends(app.db.get_session)`` and commit explicitly at the end of the
  handler.
- The worker / periodic sweep wraps a call in ``app.db.session_scope()``
  (``app.db``), which commits on clean exit.

Choosing caller-controlled commit keeps login (which both purges and issues in
one unit of work) and logout atomic from the caller's point of view, and avoids
a function committing a partially-built request transaction out from under its
caller.

Functions
---------
- :func:`issue_session` -- mint a new opaque token (login / bootstrap).
- :func:`validate_session` -- resolve a token to its :class:`User` or ``None``.
- :func:`invalidate_session` -- mark a token invalidated (logout); idempotent.
- :func:`purge_sessions` -- delete expired/invalidated rows; returns the count.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.models import Session, User, utcnow

__all__ = [
    "issue_session",
    "validate_session",
    "renew_session",
    "invalidate_session",
    "invalidate_user_sessions",
    "purge_sessions",
]

# Number of random bytes for the opaque token. ``secrets.token_urlsafe(32)``
# yields a 43-character URL-safe string carrying 256 bits of entropy -- far
# beyond any feasible guessing attack.
_TOKEN_NBYTES = 32


def _resolve_ttl_minutes(ttl_minutes: int | None) -> int:
    """Return the explicit ``ttl_minutes`` or the configured default (60)."""
    if ttl_minutes is not None:
        return ttl_minutes
    return get_settings().session_ttl_minutes


def issue_session(
    db: OrmSession,
    user_id: str,
    *,
    ttl_minutes: int | None = None,
    now: datetime | None = None,
) -> Session:
    """Mint and persist a new opaque session for ``user_id``.

    Generates a cryptographically secure, unguessable token and records a
    :class:`app.models.Session` with ``issued_at = now`` (default: current UTC),
    ``expires_at = now + ttl`` (``ttl`` defaults to
    ``Settings.session_ttl_minutes`` = 60), and ``invalidated = False``. The row
    is added and flushed into the caller's transaction but **not committed** --
    the caller commits (see module docstring).

    Args:
        db: Caller-owned SQLAlchemy session.
        user_id: The owning user's id.
        ttl_minutes: Session lifetime in minutes; defaults to settings (60).
        now: Issuance instant (timezone-aware UTC); defaults to ``utcnow()``.
            Injectable so expiry boundaries can be tested deterministically.

    Returns:
        The persisted :class:`app.models.Session` (its ``token`` is the opaque
        bearer token to hand back to the client).
    """
    if now is None:
        now = utcnow()
    ttl = _resolve_ttl_minutes(ttl_minutes)
    token = secrets.token_urlsafe(_TOKEN_NBYTES)
    session = Session(
        token=token,
        user_id=user_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=ttl),
        invalidated=False,
    )
    db.add(session)
    db.flush()
    return session


def validate_session(
    db: OrmSession,
    token: str,
    *,
    now: datetime | None = None,
) -> User | None:
    """Return the authenticated :class:`User` for ``token`` or ``None``.

    The token resolves to a user **iff** the session exists, is not
    invalidated, and ``now < expires_at``. Any other case (unknown token,
    invalidated, or ``now >= expires_at``) returns ``None``. The expiry boundary
    is exact: valid for ``now`` in ``[issued_at, expires_at)``, invalid at
    ``now == expires_at`` and after.

    Read-only: performs no writes and leaves the caller's transaction untouched.

    Args:
        db: Caller-owned SQLAlchemy session.
        token: The opaque bearer token to validate.
        now: Instant to evaluate validity at; defaults to ``utcnow()``.
            Injectable for deterministic expiry testing.

    Returns:
        The owning :class:`app.models.User`, or ``None`` if the token is not
        currently valid.
    """
    if now is None:
        now = utcnow()
    session = db.get(Session, token)
    if session is None:
        return None
    if session.invalidated:
        return None
    if now >= session.expires_at:
        return None
    return session.user


def renew_session(
    db: OrmSession,
    token: str,
    *,
    now: datetime | None = None,
    ttl_minutes: int | None = None,
    max_lifetime_seconds: int | None = None,
) -> Session | None:
    """Slide a session's expiry forward while it is actively streaming (Req 8.1/8.2).

    For a currently-valid session, extend ``expires_at`` to ``now + ttl`` (the
    base session lifetime, Req 8.1), but **never beyond** ``issued_at +
    max_lifetime_seconds`` when a cap is given (Req 8.2). Returns the session, or
    ``None`` when the token is unknown, already **invalidated** (logout takes
    precedence -- renewal never resurrects it, Req 8.3), or already expired
    (``now >= expires_at``; renewal does not raise the dead -- the client must
    re-authenticate). The change is flushed into the caller's transaction but
    **not committed** (the caller commits, per the module contract).

    Idempotent and monotonic: ``expires_at`` is only ever moved forward and never
    past the cap, so repeated calls during a long meeting keep the active session
    valid up to the bounded maximum and no further.
    """
    if now is None:
        now = utcnow()
    session = db.get(Session, token)
    if session is None or session.invalidated:
        return None
    if now >= session.expires_at:
        return None
    ttl = _resolve_ttl_minutes(ttl_minutes)
    proposed = now + timedelta(minutes=ttl)
    if max_lifetime_seconds is not None:
        cap = session.issued_at + timedelta(seconds=max_lifetime_seconds)
        proposed = min(proposed, cap)
    if proposed > session.expires_at:
        session.expires_at = proposed
        db.flush()
    return session


def invalidate_session(db: OrmSession, token: str) -> None:
    """Invalidate ``token`` (logout). Idempotent.

    Sets ``invalidated = True`` on the matching session if it exists and is not
    already invalidated. Unknown tokens and already-invalidated tokens are
    no-ops (no error). The change is flushed into the caller's transaction but
    **not committed**; the caller commits.

    Args:
        db: Caller-owned SQLAlchemy session.
        token: The opaque bearer token to invalidate.
    """
    session = db.get(Session, token)
    if session is None or session.invalidated:
        return
    session.invalidated = True
    db.flush()


def invalidate_user_sessions(
    db: OrmSession, user_id: str, *, except_token: str | None = None
) -> int:
    """Invalidate all of a user's active sessions; return the count affected.

    Marks every not-yet-invalidated session for ``user_id`` as invalidated. When
    ``except_token`` is given, that one session is preserved -- used by
    self-service password change so the caller stays signed in while other
    devices are signed out (Req 8.4). Used by the Admin_Console for disable,
    delete, password reset, and admin-revoke (Property 8). Flushed but **not
    committed** (caller-owns-commit).
    """
    stmt = update(Session).where(
        Session.user_id == user_id, Session.invalidated.is_(False)
    )
    if except_token is not None:
        stmt = stmt.where(Session.token != except_token)
    result = db.execute(stmt.values(invalidated=True))
    db.flush()
    return int(result.rowcount or 0)


def purge_sessions(db: OrmSession, *, now: datetime | None = None) -> int:
    """Delete expired or invalidated session rows; return the count deleted.

    A row is purged when it is **expired** (``now >= expires_at``) **or**
    **invalidated**. Currently-valid sessions are left untouched. Intended to be
    called opportunistically on each login and by a lightweight periodic sweep
    so the table does not grow unbounded.

    The delete executes within the caller's transaction but is **not
    committed** -- the caller commits (see module docstring). Within that same
    transaction the deleted rows are no longer visible to subsequent reads.

    Args:
        db: Caller-owned SQLAlchemy session.
        now: Instant used to decide expiry; defaults to ``utcnow()``. Injectable
            for deterministic testing.

    Returns:
        The number of rows deleted.
    """
    if now is None:
        now = utcnow()
    result = db.execute(
        delete(Session).where(
            or_(Session.expires_at <= now, Session.invalidated.is_(True))
        )
    )
    db.flush()
    # ``rowcount`` reflects the number of rows matched/deleted for this DELETE.
    return int(result.rowcount or 0)
