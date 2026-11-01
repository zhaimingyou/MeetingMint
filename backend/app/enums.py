"""Shared enumerations and domain constants (task 2.2).

These values are factored out of the ORM models so later tasks -- the Celery
worker pipeline (task 9), the status/resubmit endpoints (task 10), and the
client implementations (tasks 4-6) -- can reference a single source of truth
rather than re-spelling the literal strings.

``Processing_Job.status`` is stored as a plain string column (not a database
ENUM) to stay portable across SQLite and a possible future PostgreSQL move;
:class:`JobStatus` provides the canonical set of values and helpers used by the
application layer to validate and transition that column.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """The lifecycle states of a ``Processing_Job`` (Req 7, design state chart).

    Order of declaration mirrors the successful pipeline progression
    ``queued -> preprocessing -> transcribing -> generating -> completed`` with
    ``failed`` reachable from any active stage. ``live`` (added for
    realtime-meeting-transcription) is the entry state for a Live_Meeting_Session
    and is NOT part of the batch progression: it transitions into ``queued`` (or
    ``failed`` for a 0-byte recording) when the meeting is ended (Req 2.2, 6.2,
    6.5 of realtime-meeting-transcription).
    """

    LIVE = "live"
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    TRANSCRIBING = "transcribing"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Return all valid status strings (handy for validation/tests)."""
        return tuple(member.value for member in cls)


# Application-layer default for an Action_Item whose owner the LLM could not
# identify (Req 6.3). The database column itself is NOT-NULL; this indicator is
# substituted at the application layer when no owner is present so the column
# never has to store NULL.
UNASSIGNED_OWNER = "未指派"
