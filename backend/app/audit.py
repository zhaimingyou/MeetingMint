"""Administrative audit logging (admin-console, task 6).

Records one :class:`~app.models.AuditLogEntry` per administrative write
(configuration change, user create/modify/delete, role change, enable/disable,
password reset, job resubmit). The entry captures the acting Administrator (id
plus a username snapshot that survives the user's later deletion), the action,
the affected target, and a JSON ``detail`` of **non-secret** specifics
(Req 10.1, 10.2).

Transaction contract
--------------------
:func:`record_audit` is called **within the same transaction** as the action it
describes (``add`` + ``flush``, no commit), so the route's single ``commit``
makes the action and its audit row durable together; if the action rolls back,
no "succeeded" audit row remains (Req 10.4, Property 9). The caller owns the
commit, consistent with the rest of the codebase.

Confidentiality
--------------
Callers MUST pass only non-secret values in ``detail``. For a Configuration_Secret
change, record the key name and a ``"set"``/``"cleared"`` marker -- never the
secret value (Req 10.2).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from app.models import AuditLogEntry, User

__all__ = ["record_audit", "serialize_entry"]


def record_audit(
    db: OrmSession,
    *,
    actor: User,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Add an :class:`~app.models.AuditLogEntry` to the caller's transaction.

    Snapshots ``actor.username`` so the record remains meaningful after the
    actor is deleted (the FK is ON DELETE SET NULL). ``detail`` is JSON-encoded
    (``ensure_ascii=False`` so Chinese round-trips); pass only non-secret data.
    Flushed but **not committed** (caller-owns-commit).
    """
    entry = AuditLogEntry(
        actor_user_id=actor.id,
        actor_username=actor.username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=json.dumps(detail, ensure_ascii=False) if detail is not None else None,
    )
    db.add(entry)
    db.flush()
    return entry


def serialize_entry(entry: AuditLogEntry) -> dict[str, Any]:
    """Serialize an audit entry for an API response (parsing the JSON detail)."""
    detail: Any = None
    if entry.detail is not None:
        try:
            detail = json.loads(entry.detail)
        except (ValueError, TypeError):
            detail = entry.detail  # fall back to the raw string if not JSON
    return {
        "id": entry.id,
        "actor_username": entry.actor_username,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "detail": detail,
        "created_at": entry.created_at.isoformat(),
    }
