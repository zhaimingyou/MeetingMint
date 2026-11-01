"""Audit log API router (admin-console, task 6).

``GET /api/admin/audit`` -- returns Audit_Log_Entries newest-first in bounded
pages (Req 10.3). Admin-only via :data:`AdminUser` (an absent/invalid token is a
no-content 401, an authenticated non-administrator a 403; Req 10.5 / Req 1).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.api.admin_common import clamp_page, clamp_page_size, page_offset
from app.api.deps import AdminUser
from app.audit import serialize_entry
from app.db import get_session
from app.models import AuditLogEntry

router = APIRouter(prefix="/api/admin/audit", tags=["admin"])


@router.get("")
def list_audit(
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    page: int = 1,
    page_size: int = 50,
) -> Any:
    """Return audit entries newest-first, paged (Req 10.3).

    Ordered by ``created_at`` descending with ``id`` descending as a stable
    tie-break. ``page``/``page_size`` are clamped (page >= 1, size in [1, 200]).
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)

    total = db.execute(
        select(func.count()).select_from(AuditLogEntry)
    ).scalar_one()
    rows = (
        db.execute(
            select(AuditLogEntry)
            .order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc())
            .limit(page_size)
            .offset(page_offset(page, page_size))
        )
        .scalars()
        .all()
    )
    return JSONResponse(
        status_code=200,
        content={
            "items": [serialize_entry(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": int(total),
        },
    )
