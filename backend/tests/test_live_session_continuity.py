"""Tests for live-session authentication continuity (task 9.1; Req 8).

Covers ``app.security.sessions.renew_session`` (bounded sliding renewal) and
**Property 45**: an actively-streaming session does not expire mid-meeting (its
expiry slides forward up to a bounded maximum), while logout invalidates
immediately regardless of streaming activity.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.db import make_engine, make_session_factory
from app.models import User, create_all, utcnow
from app.security.passwords import hash_password
from app.security.sessions import (
    invalidate_session,
    issue_session,
    renew_session,
    validate_session,
)


def _fresh_factory():
    engine = make_engine("sqlite://")
    create_all(engine)
    return engine, make_session_factory(engine)


def _seed_user(sf) -> str:
    s = sf()
    try:
        u = User(username=f"u{utcnow().timestamp()}", password_hash=hash_password("p"))
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id
    finally:
        s.close()


def test_renew_extends_expiry_forward() -> None:
    engine, sf = _fresh_factory()
    try:
        uid = _seed_user(sf)
        s = sf()
        try:
            t0 = utcnow()
            sess = issue_session(s, uid, ttl_minutes=60, now=t0)
            s.commit()
            original = sess.expires_at
            # Activity 30 min later slides expiry to now+60min (> original).
            later = t0 + timedelta(minutes=30)
            renew_session(
                s, sess.token, now=later, ttl_minutes=60, max_lifetime_seconds=4 * 3600
            )
            s.commit()
            assert sess.expires_at > original
            assert sess.expires_at == later + timedelta(minutes=60)
        finally:
            s.close()
    finally:
        engine.dispose()


def test_renew_is_capped_at_max_lifetime() -> None:
    engine, sf = _fresh_factory()
    try:
        uid = _seed_user(sf)
        s = sf()
        try:
            t0 = utcnow()
            sess = issue_session(s, uid, ttl_minutes=60, now=t0)
            s.commit()
            # Activity at t0+45min (still valid: original expiry is t0+60min).
            # proposed = min(t0+45min+60min, t0+90min) = t0+90min (capped).
            max_seconds = 90 * 60
            activity = t0 + timedelta(minutes=45)
            renew_session(
                s, sess.token, now=activity, ttl_minutes=60,
                max_lifetime_seconds=max_seconds,
            )
            s.commit()
            assert sess.expires_at == t0 + timedelta(seconds=max_seconds)
        finally:
            s.close()
    finally:
        engine.dispose()


def test_renew_noop_for_invalidated_and_expired() -> None:
    engine, sf = _fresh_factory()
    try:
        uid = _seed_user(sf)
        s = sf()
        try:
            t0 = utcnow()
            sess = issue_session(s, uid, ttl_minutes=60, now=t0)
            s.commit()
            invalidate_session(s, sess.token)
            s.commit()
            # Logout precedence: renewal never resurrects an invalidated session.
            assert renew_session(s, sess.token, now=t0, max_lifetime_seconds=99999) is None
            # And an expired (but not invalidated) token is not renewed either.
            uid2 = _seed_user(sf)
            sess2 = issue_session(s, uid2, ttl_minutes=60, now=t0)
            s.commit()
            past_expiry = t0 + timedelta(minutes=120)
            assert renew_session(s, sess2.token, now=past_expiry) is None
        finally:
            s.close()
    finally:
        engine.dispose()


# Feature: realtime-meeting-transcription, Property 45: An actively-streaming live session does not expire mid-meeting, while logout still invalidates immediately.
# Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7
@settings(max_examples=100, deadline=None)
@given(
    # Inter-activity gaps in minutes (each < base ttl of 60 so the session stays
    # continuously valid while streaming), and a logout point.
    gaps=st.lists(st.integers(min_value=1, max_value=55), min_size=1, max_size=20),
)
def test_property_45_active_session_does_not_expire_then_logout_invalidates(
    gaps: list[int],
) -> None:
    engine, sf = _fresh_factory()
    try:
        uid = _seed_user(sf)
        s = sf()
        try:
            ttl = 60
            # Max lifetime far larger than the whole generated timeline, so the
            # session never hits the cap here: this isolates the "does not expire
            # mid-meeting while actively streaming" property (the cap itself is
            # covered by test_renew_is_capped_at_max_lifetime).
            max_seconds = 1000 * 3600
            t0 = utcnow()
            sess = issue_session(s, uid, ttl_minutes=ttl, now=t0)
            s.commit()
            issued = sess.issued_at

            now = t0
            for gap in gaps:
                now = now + timedelta(minutes=gap)
                # The session must still be valid at this activity instant
                # (gap < ttl and previous renewal kept it ahead).
                assert validate_session(s, sess.token, now=now) is not None
                renew_session(
                    s, sess.token, now=now, ttl_minutes=ttl,
                    max_lifetime_seconds=max_seconds,
                )
                s.commit()
                # Never extended past the bounded maximum (Req 8.2).
                assert sess.expires_at <= issued + timedelta(seconds=max_seconds)

            # Logout invalidates immediately regardless of streaming (Req 8.3).
            invalidate_session(s, sess.token)
            s.commit()
            assert validate_session(s, sess.token, now=now) is None
        finally:
            s.close()
    finally:
        engine.dispose()
