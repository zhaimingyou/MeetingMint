"""live session columns + one-live-per-owner partial unique index

Adds the realtime-meeting-transcription live-session support to
``processing_jobs``:

* ``owner_id`` (nullable) -- denormalized from the owning Meeting_Record so a
  PARTIAL UNIQUE INDEX can enforce one in-progress live session per user.
* ``last_seq`` / ``recording_bytes`` (nullable) -- the durable resume cursor.
* ``last_activity_at`` (nullable) -- drives the inactivity finalizer sweep.
* ``uq_processing_job_one_live_per_owner`` -- ``UNIQUE(owner_id) WHERE
  status='live'`` (SQLite partial index), the DB-level one-live-per-user guard
  (Req 7.2 of realtime-meeting-transcription).

All columns are nullable with no backfill: existing upload-origin rows keep
NULL and are not covered by the partial index (its predicate is status='live').

Revision ID: 0002_live_session
Revises: 0001_initial
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

import app.models  # custom UTCDateTime type used in column definitions


# revision identifiers, used by Alembic.
revision: str = "0002_live_session"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("owner_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("last_seq", sa.Integer(), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("recording_bytes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("last_activity_at", app.models.UTCDateTime(), nullable=True),
    )
    # Partial unique index: at most one row per owner while status='live'
    # (Req 7.2). SQLite supports partial indexes; ``sqlite_where`` emits the
    # WHERE predicate. Once a job leaves 'live' the predicate no longer matches,
    # so the user may start a new live meeting.
    op.create_index(
        "uq_processing_job_one_live_per_owner",
        "processing_jobs",
        ["owner_id"],
        unique=True,
        sqlite_where=sa.text("status = 'live'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_processing_job_one_live_per_owner", table_name="processing_jobs"
    )
    op.drop_column("processing_jobs", "last_activity_at")
    op.drop_column("processing_jobs", "recording_bytes")
    op.drop_column("processing_jobs", "last_seq")
    op.drop_column("processing_jobs", "owner_id")
