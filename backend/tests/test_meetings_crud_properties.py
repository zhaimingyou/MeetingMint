"""Tests for the meeting listing / detail / edit / export / delete endpoints -- task 11.

Covers the five owner-scoped CRUD endpoints added to ``app.api.meetings`` in
task 11 (Req 8.1, 8.2, 8.4, 8.5, 9.2, 9.3, 9.4, 9.6, 9.7):

- ``GET  /api/meetings``               (11.1) -- list the current user's meetings
- ``GET  /api/meetings/{id}``          (11.2) -- metadata + transcript + minutes
- ``PUT  /api/meetings/{id}/minutes``  (11.3) -- atomic edit of the minutes
- ``GET  /api/meetings/{id}/export``   (11.4) -- Markdown export
- ``DELETE /api/meetings/{id}``        (11.5) -- delete record + audio + children

It contains the ``hypothesis`` property tests for design Properties 21-26 plus
focused example tests for auth (401), 404 (unknown/foreign), the empty list, and
export-without-minutes.

Every endpoint is auth-guarded (absent/invalid token -> no-content 401, Req 1.1)
and owner-scoped: an unknown *or* foreign meeting is reported identically as
**404** so another user's data is never disclosed (Req 8.5).

Isolation
---------
``hypothesis`` disallows function-scoped fixtures under ``@given`` (examples
would share state), so every example builds a fresh, fully isolated harness via
:func:`_crud_harness`: a brand-new in-memory SQLite engine + schema, a seeded
owner + valid bearer token, a per-harness audio directory (created *inside* the
workspace and removed on exit), and a ``TestClient`` over ``create_app()`` with
the DB/settings dependencies overridden. Tokens are issued directly via
:func:`app.security.sessions.issue_session`, so the login route (and therefore
bcrypt) is never exercised here -- seeded users carry a constant placeholder
``password_hash``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

import app.api.meetings as meetings_mod
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.enums import UNASSIGNED_OWNER, JobStatus
from app.main import create_app
from app.models import (
    ActionItem,
    MeetingMinutes,
    MeetingRecord,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
    User,
    create_all,
)
from app.security.sessions import issue_session

USERNAME = "crud-owner"
OTHER_USERNAME = "crud-stranger"

# The login route is not exercised here (tokens are issued directly), so the
# password hash is irrelevant -- a constant non-null placeholder avoids paying
# for bcrypt while seeding a user per hypothesis example.
_DUMMY_PASSWORD_HASH = "not-used-token-auth-bypasses-login"

# Throwaway audio files for the delete tests are created *inside* the workspace
# (never /tmp); each harness gets its own subdirectory and removes it on exit,
# and the root is swept at interpreter exit.
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP_ROOT = os.path.join(_HERE, "_crud_tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)
atexit.register(shutil.rmtree, _TMP_ROOT, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Text / data strategies
# --------------------------------------------------------------------------- #
# Printable characters from space (U+0020) through the CJK Unified Ideographs
# block, so generated content spans ASCII and Chinese while excluding control
# characters, NUL, and surrogates (keeps SQLite TEXT round-trips and Markdown
# rendering clean). min/max_codepoint are stable across hypothesis versions.
_charset = st.characters(min_codepoint=0x20, max_codepoint=0x9FFF)
_text = st.text(alphabet=_charset, max_size=40)
_text_nonempty = st.text(alphabet=_charset, min_size=1, max_size=40)

# An owner field as a client might send it on edit: possibly blank (empty or
# whitespace-only -> must default to the unassigned indicator) or a real name.
_owner_field = st.one_of(st.just(""), st.just("   "), _text_nonempty)

_segment_strategy = st.fixed_dictionaries(
    {
        "start": st.floats(min_value=0, max_value=1.0e6, allow_nan=False, allow_infinity=False),
        "end": st.floats(min_value=0, max_value=1.0e6, allow_nan=False, allow_infinity=False),
        "text": _text,
        "speaker": st.one_of(st.none(), _text_nonempty),
    }
)

# Minutes as *persisted* (owners already non-empty, as the pipeline guarantees).
_persisted_action_item = st.fixed_dictionaries(
    {"description": _text_nonempty, "owner": _text_nonempty}
)
_persisted_minutes = st.fixed_dictionaries(
    {
        "summary": _text,
        "topics": st.lists(_text, max_size=5),
        "decisions": st.lists(_text, max_size=5),
        "action_items": st.lists(_persisted_action_item, max_size=5),
    }
)

# Minutes as *edited* via the API (owner may be blank -> normalized).
_edit_action_item = st.fixed_dictionaries(
    {"description": _text_nonempty, "owner": _owner_field}
)
_minutes_edit = st.fixed_dictionaries(
    {
        "summary": _text,
        "topics": st.lists(_text, max_size=5),
        "decisions": st.lists(_text, max_size=5),
        "action_items": st.lists(_edit_action_item, max_size=5),
    }
)


# --------------------------------------------------------------------------- #
# Seeding / reading helpers
# --------------------------------------------------------------------------- #
def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_user(session_factory, username: str) -> str:
    sess = session_factory()
    try:
        user = User(username=username, password_hash=_DUMMY_PASSWORD_HASH)
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user.id
    finally:
        sess.close()


def _issue_token(session_factory, user_id: str) -> str:
    sess = session_factory()
    try:
        token = issue_session(sess, user_id, ttl_minutes=60).token
        sess.commit()
        return token
    finally:
        sess.close()


def _seed_meeting(
    session_factory,
    *,
    owner_id: str,
    title: str = "meeting",
    created_at: datetime | None = None,
    status_value: str = JobStatus.COMPLETED.value,
    source_audio_path: str = "/data/audio/x.wav",
    standardized_audio_path: str | None = None,
) -> str:
    """Insert a Meeting_Record + its Processing_Job; return the new meeting id."""
    sess = session_factory()
    try:
        kwargs: dict[str, object] = {
            "owner_id": owner_id,
            "title": title,
            "source_audio_path": source_audio_path,
            "source_format": "wav",
            "standardized_audio_path": standardized_audio_path,
        }
        if created_at is not None:
            kwargs["created_at"] = created_at
        record = MeetingRecord(**kwargs)
        sess.add(record)
        sess.flush()
        sess.add(ProcessingJob(meeting_id=record.id, status=status_value))
        sess.commit()
        return record.id
    finally:
        sess.close()


def _seed_transcript(session_factory, meeting_id: str, segments: list[dict]) -> None:
    sess = session_factory()
    try:
        transcript = Transcript(meeting_id=meeting_id)
        sess.add(transcript)
        sess.flush()
        for ordinal, seg in enumerate(segments):
            sess.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    start_seconds=float(seg["start"]),
                    end_seconds=float(seg["end"]),
                    text=seg["text"],
                    speaker=seg.get("speaker"),
                    ordinal=ordinal,
                )
            )
        sess.commit()
    finally:
        sess.close()


def _seed_minutes(session_factory, meeting_id: str, data: dict) -> None:
    sess = session_factory()
    try:
        minutes = MeetingMinutes(
            meeting_id=meeting_id,
            summary=data["summary"],
            topics=list(data["topics"]),
            decisions=list(data["decisions"]),
        )
        sess.add(minutes)
        sess.flush()
        for ordinal, item in enumerate(data["action_items"]):
            sess.add(
                ActionItem(
                    minutes_id=minutes.id,
                    description=item["description"],
                    owner=item["owner"] or UNASSIGNED_OWNER,
                    ordinal=ordinal,
                )
            )
        sess.commit()
    finally:
        sess.close()


def _read_minutes(session_factory, meeting_id: str) -> dict | None:
    """Read minutes back as a normalized dict (action items ordered by ordinal)."""
    sess = session_factory()
    try:
        minutes = sess.execute(
            select(MeetingMinutes).where(MeetingMinutes.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if minutes is None:
            return None
        items = sorted(minutes.action_items, key=lambda a: a.ordinal)
        return {
            "summary": minutes.summary,
            "topics": list(minutes.topics),
            "decisions": list(minutes.decisions),
            "action_items": [(a.description, a.owner) for a in items],
        }
    finally:
        sess.close()


def _table_counts(session_factory) -> dict[str, int]:
    """Total row counts across every meeting-owned table (for delete tests)."""
    sess = session_factory()
    try:
        return {
            "records": sess.scalar(select(func.count()).select_from(MeetingRecord)),
            "jobs": sess.scalar(select(func.count()).select_from(ProcessingJob)),
            "transcripts": sess.scalar(select(func.count()).select_from(Transcript)),
            "segments": sess.scalar(select(func.count()).select_from(TranscriptSegment)),
            "minutes": sess.scalar(select(func.count()).select_from(MeetingMinutes)),
            "action_items": sess.scalar(select(func.count()).select_from(ActionItem)),
        }
    finally:
        sess.close()


def _normalize_owner_expected(owner: str) -> str:
    """Mirror of ``app.api.meetings._normalize_owner`` for computing expectations."""
    return owner if owner.strip() else UNASSIGNED_OWNER


def _expected_after_edit(edit: dict) -> dict:
    return {
        "summary": edit["summary"],
        "topics": list(edit["topics"]),
        "decisions": list(edit["decisions"]),
        "action_items": [
            (item["description"], _normalize_owner_expected(item["owner"]))
            for item in edit["action_items"]
        ],
    }


@contextmanager
def _crud_harness():
    """Yield ``(client, session_factory, owner_id, token, audio_dir)``.

    Fresh in-memory DB + schema, a seeded owner with a 60-minute token, a
    per-harness audio directory under the workspace, and a ``TestClient`` over
    ``create_app()`` with the DB/settings dependencies overridden. Everything is
    torn down on exit.
    """
    from fastapi.testclient import TestClient

    engine = make_engine("sqlite://")
    create_all(engine)
    session_factory = make_session_factory(engine)

    owner_id = _seed_user(session_factory, USERNAME)
    token = _issue_token(session_factory, owner_id)

    audio_dir = tempfile.mkdtemp(dir=_TMP_ROOT)

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        audio_storage_dir=audio_dir,
    )  # type: ignore[call-arg]

    app = create_app()

    def _override_get_session() -> Iterator:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    client = TestClient(app)
    try:
        yield client, session_factory, owner_id, token, audio_dir
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        shutil.rmtree(audio_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 21
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 21: For any set of users and meetings, a user's meeting list contains exactly the Meeting_Records owned by that user (empty when none), each carrying title, creation timestamp, and Job_Status, ordered by creation timestamp descending with ties broken by identifier descending.
# Validates: Requirements 8.1, 8.3, 8.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    owner_buckets=st.lists(st.integers(min_value=0, max_value=4), max_size=6),
    foreign_buckets=st.lists(st.integers(min_value=0, max_value=4), max_size=3),
)
def test_property_21_listing_returns_owner_records_ordered(
    owner_buckets: list[int], foreign_buckets: list[int]
) -> None:
    """The list contains exactly the owner's meetings (none of the stranger's),
    each with title/created_at/status, ordered created_at desc then id desc.
    The empty-owner case exercises the empty-list -> 200 [] behavior (Req 8.4)."""
    statuses = JobStatus.values()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        # Owner meetings: buckets reuse timestamps to force created_at ties.
        expected: dict[str, tuple[datetime, str, str]] = {}
        for i, bucket in enumerate(owner_buckets):
            created = base + timedelta(minutes=bucket)
            status_value = statuses[bucket % len(statuses)]
            title = f"owner-meeting-{i}"
            mid = _seed_meeting(
                session_factory,
                owner_id=owner_id,
                title=title,
                created_at=created,
                status_value=status_value,
            )
            expected[mid] = (created, title, status_value)

        # A stranger's meetings must never appear in the owner's list.
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_ids: set[str] = set()
        for j, bucket in enumerate(foreign_buckets):
            fid = _seed_meeting(
                session_factory,
                owner_id=stranger_id,
                title=f"foreign-{j}",
                created_at=base + timedelta(minutes=bucket),
            )
            foreign_ids.add(fid)

        resp = client.get("/api/meetings", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()
        returned_ids = [item["id"] for item in body]

        # Exactly the owner's records -- nothing foreign, nothing missing.
        assert set(returned_ids) == set(expected)
        assert foreign_ids.isdisjoint(returned_ids)

        # created_at desc, ties broken by id desc.
        expected_order = sorted(
            expected, key=lambda mid: (expected[mid][0], mid), reverse=True
        )
        assert returned_ids == expected_order

        # Each entry carries title, creation timestamp, and Job_Status.
        for item in body:
            created, title, status_value = expected[item["id"]]
            assert item["title"] == title
            assert item["status"] == status_value
            assert isinstance(item["created_at"], str) and item["created_at"]


# --------------------------------------------------------------------------- #
# Property 22
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 22: For any Meeting_Record accessible to the requesting user, the detail response returns its metadata and includes the Transcript and the Meeting_Minutes precisely for each that has been persisted to that record.
# Validates: Requirements 8.2
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    has_transcript=st.booleans(),
    has_minutes=st.booleans(),
    segments=st.lists(_segment_strategy, max_size=4),
    minutes_data=_persisted_minutes,
)
def test_property_22_detail_includes_transcript_and_minutes_when_persisted(
    has_transcript: bool,
    has_minutes: bool,
    segments: list[dict],
    minutes_data: dict,
) -> None:
    """Detail always returns metadata, and includes the transcript / minutes
    exactly when each has been persisted (null otherwise)."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id, title="detail-me")
        if has_transcript:
            _seed_transcript(session_factory, mid, segments)
        if has_minutes:
            _seed_minutes(session_factory, mid, minutes_data)

        resp = client.get(f"/api/meetings/{mid}", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.json()

        # Metadata always present.
        assert body["id"] == mid
        assert body["title"] == "detail-me"
        assert body["status"] == JobStatus.COMPLETED.value

        # Transcript present exactly when persisted, in stored (ordinal) order.
        if has_transcript:
            assert body["transcript"] is not None
            got = body["transcript"]["segments"]
            assert len(got) == len(segments)
            for got_seg, seed_seg in zip(got, segments):
                assert got_seg["start"] == seed_seg["start"]
                assert got_seg["end"] == seed_seg["end"]
                assert got_seg["text"] == seed_seg["text"]
                assert got_seg["speaker"] == seed_seg["speaker"]
        else:
            assert body["transcript"] is None

        # Minutes present exactly when persisted.
        if has_minutes:
            assert body["minutes"] is not None
            assert body["minutes"]["summary"] == minutes_data["summary"]
            assert body["minutes"]["topics"] == minutes_data["topics"]
            assert body["minutes"]["decisions"] == minutes_data["decisions"]
            assert body["minutes"]["action_items"] == [
                {"description": a["description"], "owner": a["owner"]}
                for a in minutes_data["action_items"]
            ]
        else:
            assert body["minutes"] is None


# --------------------------------------------------------------------------- #
# Property 23
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 23: For any meeting identifier that does not exist or is not owned by the requesting user, the Backend_API responds 404 and returns none of the record's metadata, Transcript, or Meeting_Minutes.
# Validates: Requirements 8.5
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(unknown_id=st.uuids().map(str))
def test_property_23_inaccessible_or_unknown_not_disclosed(unknown_id: str) -> None:
    """An unknown id and a fully-populated foreign meeting both yield 404 with
    no metadata / transcript / minutes leaked."""
    leak_keys = {"title", "created_at", "transcript", "minutes"}

    with _crud_harness() as (client, session_factory, _owner_id, token, _audio):
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_id = _seed_meeting(
            session_factory, owner_id=stranger_id, title="secret-meeting"
        )
        _seed_transcript(
            session_factory,
            foreign_id,
            [{"start": 0.0, "end": 1.0, "text": "secret", "speaker": None}],
        )
        _seed_minutes(
            session_factory,
            foreign_id,
            {
                "summary": "secret summary",
                "topics": ["secret topic"],
                "decisions": ["secret decision"],
                "action_items": [{"description": "secret task", "owner": "spy"}],
            },
        )

        for target in (unknown_id, foreign_id):
            resp = client.get(f"/api/meetings/{target}", headers=_auth(token))
            assert resp.status_code == 404
            assert leak_keys.isdisjoint(resp.json().keys())


# --------------------------------------------------------------------------- #
# Property 24
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 24: For any set of valid edits to a Meeting_Minutes, a successful save persists exactly those edits such that a subsequent read returns them and the save is confirmed; and for any induced save failure, the previously stored Meeting_Minutes remain unchanged and an error response is returned.
# Validates: Requirements 9.2, 9.6
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(edit_a=_minutes_edit, edit_b=_minutes_edit)
def test_property_24_minutes_edit_atomic_roundtrip(edit_a: dict, edit_b: dict) -> None:
    """A successful edit round-trips exactly (owners normalized); an induced
    persistence failure preserves the previously stored minutes and 500s."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        # Seed initial minutes so there is something to edit.
        _seed_minutes(
            session_factory,
            mid,
            {
                "summary": "initial",
                "topics": ["initial-topic"],
                "decisions": ["initial-decision"],
                "action_items": [{"description": "initial-task", "owner": "alice"}],
            },
        )

        # --- successful edit round-trips exactly ---
        resp_a = client.put(
            f"/api/meetings/{mid}/minutes", json=edit_a, headers=_auth(token)
        )
        assert resp_a.status_code == 200
        assert resp_a.json()["saved"] is True

        expected_a = _expected_after_edit(edit_a)
        assert _read_minutes(session_factory, mid) == expected_a

        # The same edits are visible through the detail endpoint.
        detail = client.get(f"/api/meetings/{mid}", headers=_auth(token)).json()
        assert detail["minutes"]["summary"] == expected_a["summary"]
        assert detail["minutes"]["topics"] == expected_a["topics"]
        assert detail["minutes"]["decisions"] == expected_a["decisions"]
        assert detail["minutes"]["action_items"] == [
            {"description": d, "owner": o} for d, o in expected_a["action_items"]
        ]

        # --- induced persistence failure preserves the prior minutes ---
        saved = meetings_mod._persist_minutes_edit

        def _boom(*args, **kwargs):
            raise RuntimeError("forced persistence failure")

        meetings_mod._persist_minutes_edit = _boom
        try:
            resp_b = client.put(
                f"/api/meetings/{mid}/minutes", json=edit_b, headers=_auth(token)
            )
        finally:
            meetings_mod._persist_minutes_edit = saved

        assert resp_b.status_code == 500
        # Unchanged: still exactly what the successful edit_a wrote.
        assert _read_minutes(session_factory, mid) == expected_a


# --------------------------------------------------------------------------- #
# Property 25
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 25: For any Meeting_Minutes, the exported document contains the summary, every topic, every decision, and every Action_Item together with its owner.
# Validates: Requirements 9.3
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(minutes_data=_persisted_minutes)
def test_property_25_export_contains_every_section(minutes_data: dict) -> None:
    """The Markdown export is served as a .md attachment and contains the
    summary, every topic, every decision, and every action item with its owner."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id, title="export-me")
        _seed_minutes(session_factory, mid, minutes_data)

        resp = client.get(f"/api/meetings/{mid}/export", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        disposition = resp.headers["content-disposition"]
        assert "attachment" in disposition.lower()
        assert ".md" in disposition

        doc = resp.text
        assert minutes_data["summary"] in doc
        for topic in minutes_data["topics"]:
            assert topic in doc
        for decision in minutes_data["decisions"]:
            assert decision in doc
        for item in minutes_data["action_items"]:
            assert item["description"] in doc
            assert item["owner"] in doc


# --------------------------------------------------------------------------- #
# Property 26
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 26: For any Meeting_Record, a confirmed deletion permanently removes the record together with its associated audio file(s), Transcript, and Meeting_Minutes, leaving no residual rows or files, and returns a deletion confirmation.
# Validates: Requirements 9.4
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    has_transcript=st.booleans(),
    has_minutes=st.booleans(),
    has_standardized=st.booleans(),
)
def test_property_26_deletion_removes_all_data(
    has_transcript: bool, has_minutes: bool, has_standardized: bool
) -> None:
    """Deleting a meeting removes its record, job, transcript (+ segments),
    minutes (+ action items) and the on-disk audio file(s), and confirms."""
    with _crud_harness() as (client, session_factory, owner_id, token, audio_dir):
        source_path = os.path.join(audio_dir, "source.wav")
        with open(source_path, "wb") as fh:
            fh.write(b"source-audio-bytes")

        standardized_path: str | None = None
        if has_standardized:
            standardized_path = os.path.join(audio_dir, "standardized.wav")
            with open(standardized_path, "wb") as fh:
                fh.write(b"standardized-audio-bytes")

        mid = _seed_meeting(
            session_factory,
            owner_id=owner_id,
            source_audio_path=source_path,
            standardized_audio_path=standardized_path,
        )
        if has_transcript:
            _seed_transcript(
                session_factory,
                mid,
                [
                    {"start": 0.0, "end": 1.0, "text": "a", "speaker": "S1"},
                    {"start": 1.0, "end": 2.0, "text": "b", "speaker": None},
                ],
            )
        if has_minutes:
            _seed_minutes(
                session_factory,
                mid,
                {
                    "summary": "s",
                    "topics": ["t1", "t2"],
                    "decisions": ["d"],
                    "action_items": [
                        {"description": "do x", "owner": "me"},
                        {"description": "do y", "owner": "you"},
                    ],
                },
            )

        resp = client.delete(f"/api/meetings/{mid}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # No residual rows in any meeting-owned table.
        assert _table_counts(session_factory) == {
            "records": 0,
            "jobs": 0,
            "transcripts": 0,
            "segments": 0,
            "minutes": 0,
            "action_items": 0,
        }

        # No residual audio files on disk.
        assert not os.path.exists(source_path)
        if standardized_path is not None:
            assert not os.path.exists(standardized_path)


# --------------------------------------------------------------------------- #
# Example tests: auth (401), 404, empty list, export-without-minutes, etc.
# --------------------------------------------------------------------------- #
def test_list_requires_authentication() -> None:
    """GET list with no token -> no-content 401 (Req 1.1)."""
    with _crud_harness() as (client, _sf, _owner, _token, _audio):
        resp = client.get("/api/meetings")
        assert resp.status_code == 401
        assert resp.content == b""


def test_detail_requires_authentication() -> None:
    with _crud_harness() as (client, session_factory, owner_id, _token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        resp = client.get(f"/api/meetings/{mid}")
        assert resp.status_code == 401
        assert resp.content == b""


def test_export_requires_authentication() -> None:
    with _crud_harness() as (client, session_factory, owner_id, _token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        resp = client.get(f"/api/meetings/{mid}/export")
        assert resp.status_code == 401
        assert resp.content == b""


def test_put_minutes_requires_authentication() -> None:
    with _crud_harness() as (client, session_factory, owner_id, _token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        resp = client.put(
            f"/api/meetings/{mid}/minutes",
            json={"summary": "x", "topics": [], "decisions": [], "action_items": []},
        )
        assert resp.status_code == 401
        assert resp.content == b""


def test_delete_requires_authentication() -> None:
    with _crud_harness() as (client, session_factory, owner_id, _token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        resp = client.delete(f"/api/meetings/{mid}")
        assert resp.status_code == 401
        assert resp.content == b""
        # The meeting still exists (unauthenticated request changed nothing).
        assert _table_counts(session_factory)["records"] == 1


def test_list_empty_returns_200_empty_list() -> None:
    """A user with no meetings gets 200 and [] (Req 8.4)."""
    with _crud_harness() as (client, _sf, _owner, token, _audio):
        resp = client.get("/api/meetings", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == []


def test_detail_unknown_meeting_returns_404() -> None:
    with _crud_harness() as (client, _sf, _owner, token, _audio):
        resp = client.get(
            "/api/meetings/00000000-0000-0000-0000-000000000000",
            headers=_auth(token),
        )
        assert resp.status_code == 404


def test_detail_foreign_meeting_returns_404() -> None:
    with _crud_harness() as (client, session_factory, _owner, token, _audio):
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_id = _seed_meeting(session_factory, owner_id=stranger_id)
        resp = client.get(f"/api/meetings/{foreign_id}", headers=_auth(token))
        assert resp.status_code == 404
        assert "title" not in resp.json()


def test_export_without_minutes_returns_409() -> None:
    """Export of a meeting with no persisted minutes -> 409 (Req 9.7).

    409 (not 404) is the documented choice: the meeting exists and is owned by
    the caller, so 404 (reserved for owner-scoping) would falsely imply the
    meeting is unknown. The request conflicts with the meeting's current state
    (no minutes generated yet)."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(
            session_factory, owner_id=owner_id, status_value=JobStatus.QUEUED.value
        )
        resp = client.get(f"/api/meetings/{mid}/export", headers=_auth(token))
        assert resp.status_code == 409
        assert "minutes" in resp.json()["detail"].lower()


def test_export_with_minutes_returns_markdown_document() -> None:
    """A concrete export carries the right headers and every section (Req 9.3)."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id, title="周会")
        _seed_minutes(
            session_factory,
            mid,
            {
                "summary": "讨论了项目进度",
                "topics": ["进度回顾", "风险"],
                "decisions": ["下周发布"],
                "action_items": [
                    {"description": "准备发布说明", "owner": "张三"},
                    {"description": "复查测试", "owner": UNASSIGNED_OWNER},
                ],
            },
        )

        resp = client.get(f"/api/meetings/{mid}/export", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert resp.headers["content-disposition"].endswith('.md"')

        doc = resp.text
        for fragment in (
            "讨论了项目进度",
            "进度回顾",
            "风险",
            "下周发布",
            "准备发布说明",
            "张三",
            "复查测试",
            UNASSIGNED_OWNER,
        ):
            assert fragment in doc


def test_put_minutes_unknown_meeting_returns_404() -> None:
    with _crud_harness() as (client, _sf, _owner, token, _audio):
        resp = client.put(
            "/api/meetings/00000000-0000-0000-0000-000000000000/minutes",
            json={"summary": "x", "topics": [], "decisions": [], "action_items": []},
            headers=_auth(token),
        )
        assert resp.status_code == 404


def test_put_minutes_without_existing_minutes_returns_409() -> None:
    """Editing a meeting that has no minutes yet -> 409 (consistent with export)."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(
            session_factory, owner_id=owner_id, status_value=JobStatus.QUEUED.value
        )
        resp = client.put(
            f"/api/meetings/{mid}/minutes",
            json={"summary": "x", "topics": [], "decisions": [], "action_items": []},
            headers=_auth(token),
        )
        assert resp.status_code == 409


def test_put_minutes_blank_owner_defaults_to_unassigned() -> None:
    """A blank action-item owner is stored as the unassigned indicator (Req 6.3)."""
    with _crud_harness() as (client, session_factory, owner_id, token, _audio):
        mid = _seed_meeting(session_factory, owner_id=owner_id)
        _seed_minutes(
            session_factory,
            mid,
            {"summary": "s", "topics": [], "decisions": [], "action_items": []},
        )
        resp = client.put(
            f"/api/meetings/{mid}/minutes",
            json={
                "summary": "edited",
                "topics": ["t"],
                "decisions": ["d"],
                "action_items": [
                    {"description": "blank owner", "owner": "   "},
                    {"description": "named owner", "owner": "李四"},
                ],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200
        stored = _read_minutes(session_factory, mid)
        assert stored["action_items"] == [
            ("blank owner", UNASSIGNED_OWNER),
            ("named owner", "李四"),
        ]


def test_delete_unknown_meeting_returns_404() -> None:
    with _crud_harness() as (client, _sf, _owner, token, _audio):
        resp = client.delete(
            "/api/meetings/00000000-0000-0000-0000-000000000000",
            headers=_auth(token),
        )
        assert resp.status_code == 404


def test_delete_foreign_meeting_returns_404_and_preserves_it() -> None:
    with _crud_harness() as (client, session_factory, _owner, token, _audio):
        stranger_id = _seed_user(session_factory, OTHER_USERNAME)
        foreign_id = _seed_meeting(session_factory, owner_id=stranger_id)
        resp = client.delete(f"/api/meetings/{foreign_id}", headers=_auth(token))
        assert resp.status_code == 404
        # The stranger's meeting is untouched.
        assert _table_counts(session_factory)["records"] == 1


def test_delete_removes_record_and_audio_file() -> None:
    """A concrete delete removes the row and the source audio file (Req 9.4)."""
    with _crud_harness() as (client, session_factory, owner_id, token, audio_dir):
        source_path = os.path.join(audio_dir, "meeting.wav")
        with open(source_path, "wb") as fh:
            fh.write(b"bytes")
        mid = _seed_meeting(
            session_factory, owner_id=owner_id, source_audio_path=source_path
        )

        resp = client.delete(f"/api/meetings/{mid}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == {"id": mid, "deleted": True}
        assert _table_counts(session_factory)["records"] == 0
        assert not os.path.exists(source_path)
