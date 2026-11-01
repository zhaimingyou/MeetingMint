"""Global processing-job monitoring API (admin-console, task 10).

Admin-only, cross-user visibility and control over processing jobs
(prefix ``/api/admin/jobs``):

- ``GET  /``                     -- list jobs across ALL users, filterable by
  status, paged, newest-first; operational metadata only (no Transcript/Minutes
  content, Req 9.6).
- ``POST /{meeting_id}/resubmit`` -- re-drive a failed job regardless of owner
  (failed -> queued + re-enqueue; non-failed -> 409), mirroring the owner-scoped
  resubmit in :mod:`app.api.meetings` minus the owner filter (Req 9.4/9.5).

All routes are gated by :data:`AdminUser`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

import app.api.meetings as meetings_mod
from app.api.admin_common import clamp_page, clamp_page_size, page_offset
from app.api.deps import AdminUser
from app.audit import record_audit
from app.db import get_session
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, User

router = APIRouter(prefix="/api/admin/jobs", tags=["admin"])


def _serialize(job: ProcessingJob, meeting: MeetingRecord, owner_username: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "meeting_id": meeting.id,
        "title": meeting.title,
        "owner_username": owner_username,
        "status": job.status,
        "created_at": meeting.created_at.isoformat(),
    }
    # Surface the error only for a failed job (consistent with the owner-scoped
    # status endpoint). Never include Transcript/Minutes content (Req 9.6).
    if job.status == JobStatus.FAILED.value:
        item["error_description"] = job.error_description
    return item


@router.get("")
def list_jobs(
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> Any:
    """List processing jobs across all users (Req 9.1/9.2/9.3).

    Joins each ``Processing_Job`` to its ``Meeting_Record`` and owning ``User``;
    optionally filters by ``status``; orders newest-first by meeting creation;
    returns bounded pages. Discloses only operational metadata (Req 9.6).
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)

    base = (
        select(ProcessingJob, MeetingRecord, User.username)
        .join(MeetingRecord, ProcessingJob.meeting_id == MeetingRecord.id)
        .join(User, MeetingRecord.owner_id == User.id)
    )
    if status is not None:
        base = base.where(ProcessingJob.status == status)

    count_stmt = (
        select(func.count())
        .select_from(ProcessingJob)
        .join(MeetingRecord, ProcessingJob.meeting_id == MeetingRecord.id)
    )
    if status is not None:
        count_stmt = count_stmt.where(ProcessingJob.status == status)
    total = db.execute(count_stmt).scalar_one()

    rows = db.execute(
        base.order_by(MeetingRecord.created_at.desc(), MeetingRecord.id.desc())
        .limit(page_size)
        .offset(page_offset(page, page_size))
    ).all()

    items = [_serialize(job, meeting, username) for job, meeting, username in rows]
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total),
        },
    )


@router.post("/{meeting_id}/resubmit")
def resubmit_job(
    meeting_id: str,
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
) -> Any:
    """Resubmit a failed job for any user (Req 9.4/9.5).

    Unknown meeting/job -> 404. A non-failed job -> 409 (status preserved). A
    failed job is reset to ``queued`` with the stale error cleared, audited, and
    re-enqueued **after the commit** via the shared enqueue seam (so the worker
    never dequeues a meeting whose reset is not yet durable).
    """
    job = db.execute(
        select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if job is None:
        return JSONResponse(status_code=404, content={"detail": "meeting not found"})

    if job.status != JobStatus.FAILED.value:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "only a failed job may be resubmitted; current status is "
                    f"'{job.status}'"
                ),
                "status": job.status,
            },
        )

    job.status = JobStatus.QUEUED.value
    job.error_description = None
    db.commit()

    # Re-enqueue after the reset is durable, via the shared seam (called as an
    # attribute so tests' monkeypatch is honored).
    try:
        meetings_mod._enqueue_processing_job(meeting_id)
    except Exception:
        # The durable queued transition happened before broker submission. Put
        # the job back into a retryable state if submission itself fails.
        job.status = JobStatus.FAILED.value
        job.error_description = "failed to enqueue processing job"
        db.commit()
        raise

    # Audit only after the external enqueue action has succeeded. A broker
    # failure therefore cannot leave a misleading success entry.
    record_audit(
        db,
        actor=admin,
        action="job.resubmit",
        target_type="meeting",
        target_id=meeting_id,
    )
    db.commit()

    return JSONResponse(
        status_code=200,
        content={"id": meeting_id, "status": JobStatus.QUEUED.value},
    )
