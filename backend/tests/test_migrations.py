"""Tests for the initial Alembic migration and the startup runner (task 2.3).

These verify that :func:`app.migrations_runner.run_migrations` applies the
initial migration to a fresh temp-file SQLite database (never ``/data``) and
produces exactly the schema described by the ORM models, including key
foreign-key and unique constraints, plus a populated ``alembic_version`` table.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from app.db import Base, make_engine
import app.models  # noqa: F401  (register all tables on Base.metadata)
from app.migrations_runner import (
    ALEMBIC_INI_PATH,
    SCRIPT_LOCATION,
    make_alembic_config,
    run_migrations,
)

# Tables the migrations must create (the full ER diagram + admin-console).
EXPECTED_TABLES = {
    "users",
    "sessions",
    "meeting_records",
    "processing_jobs",
    "transcripts",
    "transcript_segments",
    "meeting_minutes",
    "action_items",
    "runtime_config",
    "config_meta",
    "audit_log",
}


def _sqlite_url(db_path: Path) -> str:
    return "sqlite:///" + str(db_path)


def test_run_migrations_creates_all_tables(tmp_path: Path) -> None:
    """Running migrations on a temp DB creates every expected table."""
    db_path = tmp_path / "migrated.db"
    run_migrations(_sqlite_url(db_path))

    assert db_path.exists()

    engine = make_engine(_sqlite_url(db_path))
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLES.issubset(table_names)


def test_migrated_schema_matches_model_metadata(tmp_path: Path) -> None:
    """The migrated schema's table set matches Base.metadata's tables."""
    db_path = tmp_path / "migrated.db"
    run_migrations(_sqlite_url(db_path))

    engine = make_engine(_sqlite_url(db_path))
    try:
        migrated = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert migrated == set(Base.metadata.tables)


def test_alembic_version_is_populated(tmp_path: Path) -> None:
    """The alembic_version table exists and records the applied head revision."""
    db_path = tmp_path / "migrated.db"
    run_migrations(_sqlite_url(db_path))

    engine = make_engine(_sqlite_url(db_path))
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            versions = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
    finally:
        engine.dispose()

    assert versions == ["0003_admin_console"]


def test_key_constraints_exist(tmp_path: Path) -> None:
    """A representative FK (ondelete CASCADE) and unique constraint are present."""
    db_path = tmp_path / "migrated.db"
    run_migrations(_sqlite_url(db_path))

    engine = make_engine(_sqlite_url(db_path))
    try:
        inspector = inspect(engine)

        # username uniqueness on users (one-to-one / unique constraints).
        user_uniques = inspector.get_unique_constraints("users")
        assert any(uc["column_names"] == ["username"] for uc in user_uniques)

        # processing_jobs.meeting_id is the one-to-one unique link to a meeting.
        job_uniques = inspector.get_unique_constraints("processing_jobs")
        assert any(uc["column_names"] == ["meeting_id"] for uc in job_uniques)

        # sessions.user_id -> users.id with ON DELETE CASCADE.
        session_fks = inspector.get_foreign_keys("sessions")
        assert any(
            fk["referred_table"] == "users"
            and fk["constrained_columns"] == ["user_id"]
            and (fk.get("options") or {}).get("ondelete") == "CASCADE"
            for fk in session_fks
        )
    finally:
        engine.dispose()


def test_run_migrations_is_idempotent(tmp_path: Path) -> None:
    """Running migrations twice on the same DB is a no-op the second time."""
    db_path = tmp_path / "migrated.db"
    run_migrations(_sqlite_url(db_path))
    # Should not raise; already at head.
    run_migrations(_sqlite_url(db_path))

    engine = make_engine(_sqlite_url(db_path))
    try:
        assert EXPECTED_TABLES.issubset(set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


def test_alembic_paths_resolve_to_backend_root() -> None:
    """The config/script paths are absolute and exist (container-safe layout)."""
    assert ALEMBIC_INI_PATH.is_absolute()
    assert ALEMBIC_INI_PATH.is_file()
    assert SCRIPT_LOCATION.is_absolute()
    assert (SCRIPT_LOCATION / "env.py").is_file()
    assert (SCRIPT_LOCATION / "versions").is_dir()


def test_0003_admin_console_upgrade_downgrade_roundtrip(tmp_path: Path) -> None:
    """0003 is additive over the pre-feature (0002) schema and reverses cleanly.

    Upgrade to 0002 first (an existing deployment), confirm the admin-console
    objects are absent, upgrade to head and confirm the new columns/tables/index
    and the seeded config_meta row, then downgrade back to 0002 and confirm they
    are gone -- i.e. the migration is a clean additive round-trip.
    """
    db_path = tmp_path / "roundtrip.db"
    url = _sqlite_url(db_path)
    cfg = make_alembic_config(url)

    # --- Pre-feature state: head was 0002. ---
    command.upgrade(cfg, "0002_live_session")
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        assert "audit_log" not in insp.get_table_names()
        assert "is_admin" not in {c["name"] for c in insp.get_columns("users")}
    finally:
        engine.dispose()

    # --- Upgrade to 0003 (admin-console). ---
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert {"runtime_config", "config_meta", "audit_log"}.issubset(names)
        user_cols = {c["name"] for c in insp.get_columns("users")}
        assert {"is_admin", "disabled"}.issubset(user_cols)
        assert "ix_audit_log_created_at" in {
            i["name"] for i in insp.get_indexes("audit_log")
        }
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version FROM config_meta WHERE id = 1")
            ).scalar_one()
        assert version == 0
    finally:
        engine.dispose()

    # --- Downgrade back to 0002: every admin-console object is removed. ---
    command.downgrade(cfg, "0002_live_session")
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert not ({"runtime_config", "config_meta", "audit_log"} & names)
        user_cols = {c["name"] for c in insp.get_columns("users")}
        assert "is_admin" not in user_cols and "disabled" not in user_cols
    finally:
        engine.dispose()
