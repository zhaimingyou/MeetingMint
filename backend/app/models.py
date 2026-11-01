"""SQLAlchemy 2.0 ORM models (task 2.2).

These models reproduce the ER diagram from design.md -> "Data Models" using the
SQLAlchemy 2.0 typed mapping style (``Mapped`` / ``mapped_column``). They all
inherit the shared declarative :class:`app.db.Base` so a single
``Base.metadata`` describes the whole schema for the initial Alembic migration
(task 2.3) and for the test ``create_all`` helper at the bottom of this module.

Key design decisions (see design.md notes beneath the ER diagram):

- **UUID primary keys are stored as text.** SQLite has no native UUID type, so
  every ``id`` is a ``String(36)`` populated by default with ``str(uuid4())``.
  A single approach is applied consistently across all tables; see
  :func:`_uuid_pk`.
- **Timezone-aware UTC datetimes.** SQLite stores naive datetimes, so the
  :class:`UTCDateTime` ``TypeDecorator`` normalizes every value to UTC on the
  way in and re-attaches ``tzinfo=UTC`` on the way out, guaranteeing callers
  always see aware UTC datetimes regardless of backend.
- **No ``password_salt`` column.** ``User.password_hash`` holds a bcrypt hash
  whose salt is embedded in the hash string itself (Req 1.7), so a separate
  salt column would be redundant.
- **Cascade on delete.** Deleting a ``Meeting_Record`` must remove its
  ``Processing_Job``, ``Transcript`` (and its segments), and ``Meeting_Minutes``
  (and its action items) -- supporting the later delete requirement (Req 9.4).
  Each child relationship uses ORM ``cascade="all, delete-orphan"`` and each
  child foreign key declares ``ondelete="CASCADE"`` with ``passive_deletes=True``
  so the rule holds whether a delete goes through the ORM or straight SQL.
  Database-level cascades work because ``PRAGMA foreign_keys=ON`` is set per
  connection in :mod:`app.db`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import JobStatus

__all__ = [
    "UTCDateTime",
    "utcnow",
    "User",
    "Session",
    "MeetingRecord",
    "ProcessingJob",
    "Transcript",
    "TranscriptSegment",
    "MeetingMinutes",
    "ActionItem",
    "RuntimeConfigEntry",
    "ConfigMeta",
    "AuditLogEntry",
    "create_all",
    "drop_all",
]


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A ``DateTime`` that always reads/writes timezone-aware UTC values.

    SQLite drops timezone information, so without this decorator a value stored
    as aware UTC would come back naive. On bind we convert any value to UTC
    (treating a naive value as already-UTC); on result we re-attach UTC tzinfo
    so the application layer can rely on aware datetimes everywhere.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _uuid_pk() -> Mapped[str]:
    """A ``String(36)`` UUID primary key defaulted to a fresh ``uuid4`` string."""
    return mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))


class User(Base):
    """An account that owns meetings and holds sessions.

    ``password_hash`` stores a bcrypt hash (salt embedded) -- there is
    deliberately no ``password_salt`` column (Req 1.7).
    """

    __tablename__ = "users"

    id: Mapped[str] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    # Administrator privilege (admin-console). The bootstrap admin is seeded
    # with is_admin=True; ordinary accounts default to False.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    # Disabled accounts cannot authenticate and have their sessions invalidated
    # immediately on disable (admin-console). Reversible (re-enable).
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    sessions: Mapped[list[Session]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    meetings: Mapped[list[MeetingRecord]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Session(Base):
    """A server-side, authoritative auth session (Req 1).

    Valid only while it exists, is not ``invalidated``, and ``now <
    expires_at``. The opaque token string is the primary key.
    """

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    invalidated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class MeetingRecord(Base):
    """A single meeting: its source/standardized audio and metadata (Req 3, 8)."""

    __tablename__ = "meeting_records"

    id: Mapped[str] = _uuid_pk()
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    source_audio_path: Mapped[str] = mapped_column(String, nullable=False)
    source_format: Mapped[str] = mapped_column(String, nullable=False)
    standardized_audio_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    owner: Mapped[User] = relationship(back_populates="meetings")

    # One-to-one children. Deleting the meeting cascades to each (Req 9.4).
    job: Mapped[ProcessingJob | None] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    minutes: Mapped[MeetingMinutes | None] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class ProcessingJob(Base):
    """Async job tracking the pipeline state for one meeting (Req 7).

    ``status`` is one of :class:`app.enums.JobStatus`; stored as a string for
    portability. One-to-one with the meeting (``meeting_id`` is unique).
    """

    __tablename__ = "processing_jobs"

    # Partial unique index enforcing one in-progress live session per owner
    # (Req 7.2). Declared here (not only in the Alembic migration) so the
    # ``create_all`` schema used by the test suite also enforces it. The
    # ``sqlite_where`` predicate makes it a SQLite partial index; on other
    # backends the dialect kwarg is ignored.
    __table_args__ = (
        Index(
            "uq_processing_job_one_live_per_owner",
            "owner_id",
            unique=True,
            sqlite_where=text("status = 'live'"),
        ),
    )

    id: Mapped[str] = _uuid_pk()
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=JobStatus.QUEUED.value
    )
    error_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- Live-session columns (realtime-meeting-transcription) ------------
    # All nullable; meaningful only for live-origin jobs (NULL for uploads).
    #
    # ``owner_id`` is denormalized from the owning Meeting_Record so a PARTIAL
    # UNIQUE INDEX ``UNIQUE(owner_id) WHERE status='live'`` can enforce
    # one-live-session-per-user at the database level (Req 7.2). The index is
    # created in the Alembic migration (SQLite supports partial indexes); it is
    # intentionally not declared via ``__table_args__`` so the constraint lives
    # in one place and ``create_all`` test schemas stay simple.
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Resume cursor (Req 5.6, 6.1): highest CONTIGUOUS persisted Audio_Increment
    # sequence, and the recording byte length at that sequence. The fast path
    # advances these in one short txn AFTER fsyncing the appended bytes
    # (data-before-metadata), so the durable cursor is never ahead of the
    # durable recording.
    last_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recording_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last Audio_Increment/keep-alive time; the inactivity sweep reads this
    # (status=='live' AND last_activity_at < now - timeout) (Req 7.3).
    last_activity_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    meeting: Mapped[MeetingRecord] = relationship(back_populates="job")


class Transcript(Base):
    """The assembled, time-ordered transcript for a meeting (Req 5)."""

    __tablename__ = "transcripts"

    id: Mapped[str] = _uuid_pk()
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )

    meeting: Mapped[MeetingRecord] = relationship(back_populates="transcript")
    segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TranscriptSegment(Base):
    """A single time-bounded segment of a transcript (Req 5.3, 5.4).

    ``ordinal`` plus ``start_seconds`` give stable ascending ordering;
    ``speaker`` is nullable since diarization data may be absent.
    """

    __tablename__ = "transcript_segments"

    id: Mapped[str] = _uuid_pk()
    transcript_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    transcript: Mapped[Transcript] = relationship(back_populates="segments")


class MeetingMinutes(Base):
    """Generated minutes for a meeting (Req 6).

    ``topics`` and ``decisions`` are JSON string arrays; ``summary`` is text.
    Action items are normalized into their own table.
    """

    __tablename__ = "meeting_minutes"

    id: Mapped[str] = _uuid_pk()
    meeting_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    meeting: Mapped[MeetingRecord] = relationship(back_populates="minutes")
    action_items: Mapped[list[ActionItem]] = relationship(
        back_populates="minutes",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ActionItem(Base):
    """A task entry within the minutes (Req 6.3).

    ``owner`` is NOT-NULL; the application substitutes the unassigned indicator
    (``app.enums.UNASSIGNED_OWNER``) when the LLM identifies no owner, so the
    column never stores NULL.
    """

    __tablename__ = "action_items"

    id: Mapped[str] = _uuid_pk()
    minutes_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("meeting_minutes.id", ondelete="CASCADE"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    minutes: Mapped[MeetingMinutes] = relationship(back_populates="action_items")


# --------------------------------------------------------------------------- #
# Admin console models (admin-console feature)
# --------------------------------------------------------------------------- #
class RuntimeConfigEntry(Base):
    """A persisted runtime override for one runtime-editable configuration key.

    The ``key`` is a field name from the ``RUNTIME_EDITABLE`` registry
    (``app.config_store``); ``value`` is the stringified setting value (mirroring
    how ``pydantic-settings`` coerces env strings), or Fernet ciphertext when
    ``is_secret`` is true (currently only ``llm_api_key``). Absence of a row for
    a key means "use the environment default" -- so an empty table reproduces the
    current env-only behavior exactly. Writes bump ``ConfigMeta.version`` in the
    same transaction so a reader observing a new version also sees these rows.
    """

    __tablename__ = "runtime_config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    # Who last changed this key; SET NULL on user delete so the row survives.
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ConfigMeta(Base):
    """Single-row table holding the monotonic runtime-configuration version.

    Exactly one row (``id = 1``). ``version`` is incremented in the same
    transaction as any ``runtime_config`` write, so the Backend_API and
    Job_Processor can detect a change with one cheap ``SELECT version`` and
    rebuild their effective configuration without a restart.
    """

    __tablename__ = "config_meta"
    __table_args__ = (CheckConstraint("id = 1", name="ck_config_meta_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class AuditLogEntry(Base):
    """An immutable record of one administrative write action (admin-console).

    Captures the acting Administrator (id plus a username snapshot that survives
    the user's deletion), the action, the affected target, a timestamp, and a
    JSON ``detail`` of non-secret specifics. No Configuration_Secret or password
    value is ever stored here. Written within the same transaction as the action
    it describes, so a rolled-back action leaves no "succeeded" audit row.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = _uuid_pk()
    # SET NULL on user delete; actor_username preserves who acted.
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_username: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String, nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )


def create_all(engine: object) -> None:
    """Create every table described by ``Base.metadata`` on ``engine``.

    Intended for tests and ad-hoc setup. Production schema management goes
    through Alembic migrations (task 2.3), not this helper.
    """
    Base.metadata.create_all(bind=engine)  # type: ignore[arg-type]


def drop_all(engine: object) -> None:
    """Drop every table described by ``Base.metadata`` on ``engine`` (test use)."""
    Base.metadata.drop_all(bind=engine)  # type: ignore[arg-type]
