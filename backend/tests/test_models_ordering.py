"""Unit tests for meeting listing order and extra model-persistence coverage (task 2.4).

The headline behavior under test is the meeting listing order that Requirement
8.3 depends on::

    ORDER BY created_at DESC, id DESC

That is: newest ``created_at`` first, and when two meetings share the same
``created_at`` the tie is broken by the ``Meeting_Record`` identifier
*descending*. Ids are UUID strings stored as ``String(36)``, so the tie-break is
a lexicographic comparison on the string id, descending -- this module asserts
exactly that semantics, both via the reusable :func:`app.queries.meetings_ordered`
helper and via an explicit inline ``order_by`` so the two agree.

A few quick model-persistence checks not already covered by ``test_models`` are
included here too (Session expiry/invalidation fields, ProcessingJob default
status, and ``updated_at`` ``onupdate`` behavior).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.db import make_engine, make_session_factory
from app.enums import JobStatus
from app.models import (
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    Session,
    User,
    create_all,
)
from app.queries import meetings_ordered, ordered_by_listing


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


def _add_meeting(
    session: OrmSession,
    owner: User,
    *,
    title: str,
    created_at: datetime,
    id_: str | None = None,
) -> MeetingRecord:
    kwargs: dict[str, object] = {
        "owner_id": owner.id,
        "title": title,
        "source_audio_path": f"/data/audio/{title}.wav",
        "source_format": "wav",
        "created_at": created_at,
    }
    if id_ is not None:
        kwargs["id"] = id_
    meeting = MeetingRecord(**kwargs)
    session.add(meeting)
    session.commit()
    return meeting


# --------------------------------------------------------------------------- #
# Listing order: created_at DESC                                              #
# --------------------------------------------------------------------------- #


def test_listing_distinct_created_at_returns_newest_first(session: OrmSession) -> None:
    """Distinct created_at => strictly newest-first ordering (Req 8.3)."""
    user = _make_user(session)
    base = datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)
    # Insert deliberately out of chronological order to prove ordering isn't
    # just insertion order.
    oldest = _add_meeting(session, user, title="oldest", created_at=base)
    newest = _add_meeting(
        session, user, title="newest", created_at=base + timedelta(hours=2)
    )
    middle = _add_meeting(
        session, user, title="middle", created_at=base + timedelta(hours=1)
    )

    rows = session.execute(meetings_ordered()).scalars().all()
    assert [m.id for m in rows] == [newest.id, middle.id, oldest.id]
    # created_at must be monotonically non-increasing.
    times = [m.created_at for m in rows]
    assert times == sorted(times, reverse=True)


def test_helper_matches_inline_order_by(session: OrmSession) -> None:
    """The reusable helper produces the same order as an explicit inline order_by."""
    user = _make_user(session)
    base = datetime(2024, 5, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(5):
        _add_meeting(
            session, user, title=f"m{i}", created_at=base + timedelta(minutes=i)
        )

    via_helper = [
        m.id for m in session.execute(meetings_ordered()).scalars().all()
    ]
    inline = [
        m.id
        for m in session.execute(
            select(MeetingRecord).order_by(
                MeetingRecord.created_at.desc(), MeetingRecord.id.desc()
            )
        )
        .scalars()
        .all()
    ]
    assert via_helper == inline


# --------------------------------------------------------------------------- #
# Listing order tie-break: id DESC (lexicographic on UUID string)             #
# --------------------------------------------------------------------------- #


def test_listing_same_created_at_tie_break_is_id_desc(session: OrmSession) -> None:
    """Equal created_at => tie broken by id descending, lexicographically.

    Ids are UUID *strings*; we assign explicit, lexicographically-ordered ids so
    the expected order is unambiguous and independent of random uuid4 values.
    """
    user = _make_user(session)
    tie = datetime(2024, 3, 3, 10, 0, tzinfo=timezone.utc)
    # Three meetings sharing one created_at, with controlled string ids.
    _add_meeting(session, user, title="a", created_at=tie, id_="aaaaaaaa")
    _add_meeting(session, user, title="m", created_at=tie, id_="mmmmmmmm")
    _add_meeting(session, user, title="z", created_at=tie, id_="zzzzzzzz")

    rows = session.execute(meetings_ordered()).scalars().all()
    ids = [m.id for m in rows]
    # Descending lexicographic on the string id.
    assert ids == ["zzzzzzzz", "mmmmmmmm", "aaaaaaaa"]
    assert ids == sorted(ids, reverse=True)


def test_tie_break_lexicographic_not_numeric(session: OrmSession) -> None:
    """Tie-break is string (lexicographic), not numeric, comparison.

    "10" sorts BEFORE "9" lexicographically, so descending order puts "9" first.
    This pins the semantics to the stored ``String(36)`` id, matching how UUID
    text ids compare.
    """
    user = _make_user(session)
    tie = datetime(2024, 4, 4, 11, 0, tzinfo=timezone.utc)
    _add_meeting(session, user, title="nine", created_at=tie, id_="9")
    _add_meeting(session, user, title="ten", created_at=tie, id_="10")

    rows = session.execute(meetings_ordered()).scalars().all()
    # Lexicographic desc: "9" > "10" because '9' > '1' at the first character.
    assert [m.id for m in rows] == ["9", "10"]


def test_listing_combined_created_at_then_id_tie_break(session: OrmSession) -> None:
    """Primary created_at DESC with id DESC only applied within a tie group."""
    user = _make_user(session)
    t_new = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    t_old = datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)
    # Newer group (two share t_new), older group (two share t_old).
    _add_meeting(session, user, title="new-a", created_at=t_new, id_="new-aaa")
    _add_meeting(session, user, title="new-b", created_at=t_new, id_="new-bbb")
    _add_meeting(session, user, title="old-a", created_at=t_old, id_="old-aaa")
    _add_meeting(session, user, title="old-b", created_at=t_old, id_="old-bbb")

    ids = [m.id for m in session.execute(meetings_ordered()).scalars().all()]
    # Newer group first (each internally id DESC), then older group (id DESC).
    assert ids == ["new-bbb", "new-aaa", "old-bbb", "old-aaa"]


def test_ordered_by_listing_preserves_caller_where(session: OrmSession) -> None:
    """ordered_by_listing applies ordering on top of a caller-built (scoped) select."""
    user = _make_user(session, "owner1")
    other = _make_user(session, "owner2")
    tie = datetime(2024, 7, 7, 7, 0, tzinfo=timezone.utc)
    _add_meeting(session, user, title="mine-1", created_at=tie, id_="mine-111")
    _add_meeting(session, user, title="mine-2", created_at=tie, id_="mine-222")
    _add_meeting(session, other, title="theirs", created_at=tie, id_="theirs-9")

    scoped = ordered_by_listing(
        select(MeetingRecord).where(MeetingRecord.owner_id == user.id)
    )
    rows = session.execute(scoped).scalars().all()
    ids = [m.id for m in rows]
    # Only owner1's records, ordered id DESC within the created_at tie.
    assert ids == ["mine-222", "mine-111"]


# --------------------------------------------------------------------------- #
# Extra model-persistence coverage                                            #
# --------------------------------------------------------------------------- #


def test_session_expiry_and_invalidation_fields_persist(session: OrmSession) -> None:
    """Session issued_at/expires_at/invalidated round-trip with aware UTC times."""
    user = _make_user(session)
    issued = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    expires = datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc)
    session.add(
        Session(
            token="tok-1",
            user_id=user.id,
            issued_at=issued,
            expires_at=expires,
            invalidated=True,
        )
    )
    session.commit()
    session.expire_all()

    fetched = session.get(Session, "tok-1")
    assert fetched is not None
    assert fetched.issued_at == issued
    assert fetched.expires_at == expires
    assert fetched.expires_at.tzinfo is not None
    assert fetched.invalidated is True


def test_processing_job_defaults_to_queued(session: OrmSession) -> None:
    """A ProcessingJob created without an explicit status defaults to queued (Req 7.1)."""
    user = _make_user(session)
    meeting = _add_meeting(
        session,
        user,
        title="m",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    job = ProcessingJob(meeting_id=meeting.id)
    session.add(job)
    session.commit()
    session.expire_all()

    fetched = session.get(ProcessingJob, job.id)
    assert fetched is not None
    assert fetched.status == JobStatus.QUEUED.value
    assert fetched.error_description is None


def test_minutes_updated_at_changes_on_update(session: OrmSession) -> None:
    """MeetingMinutes.updated_at advances on update (onupdate=utcnow)."""
    user = _make_user(session)
    meeting = _add_meeting(
        session,
        user,
        title="m",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    minutes = MeetingMinutes(meeting_id=meeting.id, summary="initial")
    session.add(minutes)
    session.commit()
    first_updated = minutes.updated_at
    assert first_updated is not None

    # Ensure the clock advances enough to observe a difference.
    time.sleep(0.01)
    minutes.summary = "edited"
    session.commit()
    session.refresh(minutes)

    assert minutes.summary == "edited"
    assert minutes.updated_at >= first_updated
    assert minutes.updated_at > first_updated
