"""Reusable data-layer query builders (task 2.4).

These return SQLAlchemy ``Select`` statements that encode query *semantics*
shared across the application so the rules live in one place rather than being
re-spelled at each call site. They are deliberately data-layer only: they build
statements, they do not execute them or touch the API.

The first such rule is the meeting listing order. Per design.md ("Data Models"
notes) and Requirement 8.3, meetings are listed::

    ORDER BY created_at DESC, id DESC

i.e. newest first, with ties (equal ``created_at``) broken by the
``Meeting_Record`` identifier descending. Because ids are stored as UUID *text*
(``String(36)``), the id tie-break is a lexicographic comparison on the string
value descending -- a deterministic, stable ordering. The owner-scoping and the
endpoint itself are built later (task 11.1); this helper just provides the
canonical ordering used there.
"""

from __future__ import annotations

from sqlalchemy import Select, select

from app.models import MeetingRecord

__all__ = ["meetings_ordered", "ordered_by_listing"]


def ordered_by_listing(stmt: Select) -> Select:
    """Apply the canonical meeting listing order to ``stmt``.

    Orders by ``created_at`` descending, breaking ties by ``id`` descending
    (lexicographic on the UUID string), per Requirement 8.3. Accepts an existing
    ``Select`` so callers can first add owner-scoping / column projections (task
    11.1) and then apply the shared ordering here.
    """
    return stmt.order_by(MeetingRecord.created_at.desc(), MeetingRecord.id.desc())


def meetings_ordered() -> Select:
    """Return ``select(MeetingRecord)`` ordered for listing (Req 8.3).

    Convenience wrapper over :func:`ordered_by_listing` for the common
    "all meeting records, newest first" select. Callers add ``.where(...)`` for
    owner-scoping as needed.
    """
    return ordered_by_listing(select(MeetingRecord))
