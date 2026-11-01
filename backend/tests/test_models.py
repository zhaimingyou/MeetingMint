"""Unit tests for the ORM models (task 2.2).

These build the schema on a fresh in-memory SQLite engine via
``app.db.make_engine`` + ``app.models.create_all`` and verify:

- a ``User`` round-trips, including a timezone-aware UTC ``created_at``;
- a full ``MeetingRecord`` graph (job, transcript + segments, minutes + action
  items) persists and its relationships are navigable;
- deleting a ``MeetingRecord`` cascades to all of its children;
- the ``username`` unique constraint is enforced;
- the JSON ``topics`` / ``decisions`` columns round-trip Python lists;
- the ``owner`` column is NOT-NULL.

Ordering/listing behavior is intentionally left to task 2.4.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.db import make_engine, make_session_factory
from app.enums import UNASSIGNED_OWNER, JobStatus
from app.models import (
    ActionItem,
    AuditLogEntry,
    ConfigMeta,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    RuntimeConfigEntry,
    Session,
    Transcript,
    TranscriptSegment,
    User,
    create_all,
)


@pytest.fixture()
def session() -> OrmSession:
    """A session over a fresh in-memory schema; disposed after the test."""
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _make_user(session: OrmSession, username: str = "alice") -> User:
    user = User(username=username, password_hash="bcrypt$hash")
    session.add(user)
    session.commit()
    return user


def test_user_round_trips_with_aware_utc_created_at(session: OrmSession) -> None:
    user = _make_user(session)

    fetched = session.get(User, user.id)
    assert fetched is not None
    assert fetched.username == "alice"
    assert fetched.password_hash == "bcrypt$hash"
    # id was auto-populated with a uuid4 string.
    assert isinstance(fetched.id, str) and len(fetched.id) == 36
    # created_at is timezone-aware UTC.
    assert fetched.created_at.tzinfo is not None
    assert fetched.created_at.utcoffset() == timezone.utc.utcoffset(None)


def test_user_has_no_password_salt_column() -> None:
    """The schema must not contain a password_salt column (Req 1.7 note)."""
    assert "password_salt" not in User.__table__.columns


def test_username_unique_constraint(session: OrmSession) -> None:
    _make_user(session, "bob")
    session.add(User(username="bob", password_hash="other"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_session_persists_and_links_user(session: OrmSession) -> None:
    user = _make_user(session)
    issued = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires = datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc)
    token = Session(
        token="opaque-token-123",
        user_id=user.id,
        issued_at=issued,
        expires_at=expires,
    )
    session.add(token)
    session.commit()

    fetched = session.get(Session, "opaque-token-123")
    assert fetched is not None
    assert fetched.invalidated is False  # default
    assert fetched.user.id == user.id
    assert fetched.expires_at == expires
    assert fetched in user.sessions


def _build_full_meeting_graph(session: OrmSession, owner: User) -> MeetingRecord:
    meeting = MeetingRecord(
        owner_id=owner.id,
        title="周会",
        source_audio_path="/data/audio/src.webm",
        source_format="webm",
    )
    meeting.job = ProcessingJob(status=JobStatus.COMPLETED.value)
    transcript = Transcript()
    transcript.segments = [
        TranscriptSegment(
            start_seconds=0.0,
            end_seconds=2.5,
            text="大家好",
            speaker="说话人1",
            ordinal=0,
        ),
        TranscriptSegment(
            start_seconds=2.5,
            end_seconds=5.0,
            text="开始开会",
            speaker=None,
            ordinal=1,
        ),
    ]
    meeting.transcript = transcript
    minutes = MeetingMinutes(
        summary="会议摘要",
        topics=["议题一", "议题二"],
        decisions=["决定一"],
    )
    minutes.action_items = [
        ActionItem(description="跟进预算", owner="张三", ordinal=0),
        ActionItem(description="发送纪要", owner=UNASSIGNED_OWNER, ordinal=1),
    ]
    meeting.minutes = minutes
    session.add(meeting)
    session.commit()
    return meeting


def test_full_meeting_graph_persists_and_navigates(session: OrmSession) -> None:
    user = _make_user(session)
    meeting = _build_full_meeting_graph(session, user)
    meeting_id = meeting.id
    session.expire_all()

    fetched = session.get(MeetingRecord, meeting_id)
    assert fetched is not None
    assert fetched.owner.id == user.id
    assert fetched.job is not None
    assert fetched.job.status == JobStatus.COMPLETED.value

    # Transcript + segments navigable.
    assert fetched.transcript is not None
    seg_texts = {s.ordinal: s.text for s in fetched.transcript.segments}
    assert seg_texts == {0: "大家好", 1: "开始开会"}
    speakers = {s.ordinal: s.speaker for s in fetched.transcript.segments}
    assert speakers == {0: "说话人1", 1: None}

    # Minutes + JSON lists + action items navigable.
    assert fetched.minutes is not None
    assert fetched.minutes.summary == "会议摘要"
    assert fetched.minutes.topics == ["议题一", "议题二"]
    assert fetched.minutes.decisions == ["决定一"]
    owners = {ai.ordinal: ai.owner for ai in fetched.minutes.action_items}
    assert owners == {0: "张三", 1: UNASSIGNED_OWNER}


def test_json_columns_round_trip_lists(session: OrmSession) -> None:
    user = _make_user(session)
    meeting = _build_full_meeting_graph(session, user)
    minutes_id = meeting.minutes.id
    session.expire_all()

    fetched = session.get(MeetingMinutes, minutes_id)
    assert fetched is not None
    assert isinstance(fetched.topics, list)
    assert isinstance(fetched.decisions, list)
    assert fetched.topics == ["议题一", "议题二"]


def test_deleting_meeting_cascades_to_all_children(session: OrmSession) -> None:
    user = _make_user(session)
    meeting = _build_full_meeting_graph(session, user)
    session.expire_all()

    session.delete(session.get(MeetingRecord, meeting.id))
    session.commit()

    # All children gone; the user remains.
    assert session.execute(select(ProcessingJob)).first() is None
    assert session.execute(select(Transcript)).first() is None
    assert session.execute(select(TranscriptSegment)).first() is None
    assert session.execute(select(MeetingMinutes)).first() is None
    assert session.execute(select(ActionItem)).first() is None
    assert session.get(User, user.id) is not None


def test_action_item_owner_is_not_null(session: OrmSession) -> None:
    user = _make_user(session)
    meeting = MeetingRecord(
        owner_id=user.id,
        title="t",
        source_audio_path="/p",
        source_format="wav",
    )
    meeting.minutes = MeetingMinutes(summary="s")
    meeting.minutes.action_items = [ActionItem(description="d", owner=None, ordinal=0)]
    session.add(meeting)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------- #
# Admin-console models (admin-console feature)
# --------------------------------------------------------------------------- #
def test_user_admin_and_disabled_default_false(session: OrmSession) -> None:
    """New User flags default to False for an ordinary account."""
    user = _make_user(session, "carol")
    fetched = session.get(User, user.id)
    assert fetched is not None
    assert fetched.is_admin is False
    assert fetched.disabled is False


def test_runtime_config_entry_round_trips_and_actor_set_null_on_user_delete(
    session: OrmSession,
) -> None:
    """A runtime_config row round-trips; deleting its updater nulls the FK."""
    user = _make_user(session, "editor")
    session.add(
        RuntimeConfigEntry(
            key="llm_model_name",
            value="deepseek-v4-flash",
            is_secret=False,
            updated_by_user_id=user.id,
        )
    )
    session.commit()

    row = session.get(RuntimeConfigEntry, "llm_model_name")
    assert row is not None
    assert row.value == "deepseek-v4-flash"
    assert row.is_secret is False
    assert row.updated_by_user_id == user.id

    # ON DELETE SET NULL: the override survives the user's deletion.
    session.delete(user)
    session.commit()
    session.expire_all()
    row = session.get(RuntimeConfigEntry, "llm_model_name")
    assert row is not None
    assert row.updated_by_user_id is None


def test_config_meta_singleton_check_constraint(session: OrmSession) -> None:
    """config_meta enforces a single row (id must equal 1)."""
    session.add(ConfigMeta(id=1, version=0))
    session.commit()
    # A second row with id != 1 violates the CHECK constraint.
    session.add(ConfigMeta(id=2, version=0))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_audit_log_actor_set_null_preserves_username(session: OrmSession) -> None:
    """Deleting the actor nulls actor_user_id but keeps the username snapshot."""
    admin = _make_user(session, "root")
    session.add(
        AuditLogEntry(
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="config.update",
            target_type="config",
            target_id=None,
            detail='{"keys": ["llm_model_name"]}',
        )
    )
    session.commit()
    entry_id = session.execute(select(AuditLogEntry.id)).scalar_one()

    session.delete(admin)
    session.commit()
    session.expire_all()

    entry = session.get(AuditLogEntry, entry_id)
    assert entry is not None
    assert entry.actor_user_id is None  # SET NULL
    assert entry.actor_username == "root"  # snapshot preserved
    assert entry.action == "config.update"
