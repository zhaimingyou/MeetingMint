"""admin console: user role/disabled columns + runtime config + audit log

Adds the admin-console support, all **additive** so an existing ``app.db`` on the
shared volume upgrades in place with no data loss:

* ``users.is_admin`` / ``users.disabled`` -- NOT NULL booleans defaulting to 0
  for every existing row (server_default). The configured bootstrap admin is
  (re)promoted to ``is_admin=True`` at startup by
  ``app.security.bootstrap.ensure_bootstrap_admin`` so upgrading a pre-feature
  deployment never locks the operator out; this migration only backfills the
  column default.
* ``runtime_config`` -- key/value overrides for runtime-editable settings
  (value is Fernet ciphertext when ``is_secret``); an empty table reproduces the
  current env-only behavior.
* ``config_meta`` -- single row (``id=1``) holding the monotonic config
  ``version`` bumped in the same transaction as any ``runtime_config`` write;
  seeded here at ``version=0``.
* ``audit_log`` -- one row per administrative write; ``created_at`` indexed for
  newest-first paging. FK actor uses ON DELETE SET NULL with a username snapshot
  so entries survive the actor's deletion.

Revision ID: 0003_admin_console
Revises: 0002_live_session
Create Date: 2026-06-19

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

import app.models  # custom UTCDateTime type used in column definitions


# revision identifiers, used by Alembic.
revision: str = "0003_admin_console"
down_revision: str | None = "0002_live_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- users: role + disabled flags (NOT NULL, default false) -----------
    op.add_column(
        "users",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "disabled", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
    )

    # --- runtime_config: persisted overrides for runtime-editable settings -
    op.create_table(
        "runtime_config",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "is_secret", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("updated_at", app.models.UTCDateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )

    # --- config_meta: single-row monotonic version counter ----------------
    op.create_table(
        "config_meta",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=False, nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("id = 1", name="ck_config_meta_singleton"),
    )
    # Seed the single row so readers always find version 0 before any change.
    op.execute("INSERT INTO config_meta (id, version) VALUES (1, 0)")

    # --- audit_log: one row per administrative write ----------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_username", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("config_meta")
    op.drop_table("runtime_config")
    op.drop_column("users", "disabled")
    op.drop_column("users", "is_admin")
