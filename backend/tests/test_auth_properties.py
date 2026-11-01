"""Property-based tests for authentication & session management (task 3.6).

These are the *universal* (Hypothesis) versions of the auth correctness
properties from design.md -> "Correctness Properties". They complement the
example/integration tests already present in ``tests/test_passwords.py``,
``tests/test_sessions.py``, ``tests/test_auth_dependency.py``, and
``tests/test_auth_api.py`` (which cover concrete cases / boundaries) by
exercising a wide input space, each property running >= 100 examples.

Covered properties (exact text reproduced in the per-test tags below):

- Property 1: credential hashing is one-way and salted (Req 1.7) -- pure
  function test against :mod:`app.security.passwords`.
- Property 2: session validity follows issuance, expiry, and invalidation
  (Req 1.2, 1.5, 1.6) -- against :mod:`app.security.sessions` with an injected
  ``now`` and an in-memory database, so expiry boundaries are exact and no test
  ever sleeps.
- Property 3: protected endpoints reject unauthenticated access (Req 1.1) --
  driven through a FastAPI ``TestClient`` against a test-only protected probe
  route (mirroring ``tests/test_auth_dependency.py``) with generated
  absent/malformed/unknown/expired/invalidated credentials; asserts 401 + empty
  body.
- Property 4: login rejects invalid credentials and out-of-bounds fields
  (Req 1.3, 1.4) -- driven through ``POST /api/auth/login`` with generated
  non-matching credential pairs (-> 401) and empty/over-256 fields (-> 400),
  asserting no session row is created in either case.

bcrypt note (Property 1): bcrypt only considers the first 72 UTF-8 bytes of a
password (documented truncation), so when we need a "different" password to
fail verification we constrain it to differ from the original *within the first
72 bytes* via an ``assume``; otherwise two passwords that share their first 72
bytes would (correctly) verify against the same hash.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.api.deps import CurrentUser
from app.config import Settings, get_settings
from app.db import get_session, make_engine, make_session_factory
from app.models import Session as SessionModel
from app.models import User, create_all, utcnow
from app.main import create_app
from app.security.passwords import hash_password, verify_password
from app.security.sessions import (
    invalidate_session,
    issue_session,
    validate_session,
)

# bcrypt considers at most the first 72 UTF-8 bytes of a password.
_BCRYPT_MAX_BYTES = 72

# Text restricted to characters that always UTF-8 encode (no lone surrogates),
# so bcrypt hashing never raises and JSON bodies always serialize. Still spans
# the full unicode range incl. CJK / emoji for diverse coverage.
_utf8_text = st.text(alphabet=st.characters(codec="utf-8"), max_size=200)


# ===========================================================================
# Property 1: Credential hashing is one-way and salted
# ===========================================================================


# Feature: meeting-minutes-service, Property 1: For any password string, the stored credential is not the plaintext, verification of the correct password against the stored hash succeeds, verification of any different password fails, and two users with the same password receive different stored hashes (distinct salts).
# Validates: Requirements 1.7
@settings(max_examples=100, deadline=None)
@given(password=_utf8_text)
def test_property1_hashing_is_one_way_and_salted(password: str) -> None:
    """Hashing hides the plaintext, verifies the correct password, rejects a
    different one, and uses a distinct salt per call."""
    hashed = hash_password(password)

    # One-way / not plaintext: the stored credential is not the plaintext and
    # is a genuine bcrypt hash (so the plaintext cannot be read off it). A
    # substring check is deliberately NOT used: a short password built from the
    # base64 alphabet can coincidentally occur inside the hash string, which
    # would not weaken the one-way guarantee.
    assert hashed != password
    assert hashed.startswith("$2")

    # The correct password verifies against its own hash.
    assert verify_password(password, hashed) is True

    # Distinct salts: hashing the SAME password again yields a different stored
    # hash, yet that second hash also verifies the same password.
    hashed_again = hash_password(password)
    assert hashed_again != hashed
    assert verify_password(password, hashed_again) is True

    # A different password fails verification. bcrypt only considers the first
    # 72 bytes, so we construct a candidate that is guaranteed to differ from
    # the password WITHIN that window by changing its first byte -- otherwise a
    # password sharing the first 72 bytes would (correctly) verify.
    sentinel = "Z" if password[:1] != "Z" else "Q"
    different = sentinel + password
    assert (
        different.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        != password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    )
    assert verify_password(different, hashed) is False


# ===========================================================================
# Property 2: Session validity follows issuance, expiry, and invalidation
# ===========================================================================


# Issuance instants restricted to a safe range so adding the TTL / probe offset
# can never overflow ``datetime`` arithmetic. Timezone-aware UTC throughout.
_issuance_times = st.datetimes(
    min_value=datetime(2000, 1, 1, 0, 0, 0),
    max_value=datetime(2100, 1, 1, 0, 0, 0),
    timezones=st.just(timezone.utc),
)


@pytest.fixture(scope="module")
def session_env() -> Iterator[SimpleNamespace]:
    """A module-scoped in-memory DB + a seeded user for session property tests.

    Module-scoped so Hypothesis can reuse it across examples without tripping
    the function-scoped-fixture health check; each example issues its own unique
    token, so reuse is safe (no cross-example interference).
    """
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    user = User(username="prop2-user", password_hash=hash_password("pw"))
    sess.add(user)
    sess.commit()
    user_id = user.id
    sess.close()
    try:
        yield SimpleNamespace(factory=factory, user_id=user_id)
    finally:
        engine.dispose()


# Feature: meeting-minutes-service, Property 2: For any established session with issuance time t, the session token validates as authentic at any instant in [t, t+60min), and does not validate at any instant >= t+60min or after the session has been invalidated by logout.
# Validates: Requirements 1.2, 1.5, 1.6
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(t=_issuance_times, ttl_minutes=st.integers(min_value=1, max_value=600), data=st.data())
def test_property2_session_validity_expiry_invalidation(
    session_env: SimpleNamespace,
    t: datetime,
    ttl_minutes: int,
    data: st.DataObject,
) -> None:
    """A session validates exactly on [t, t+ttl) and never once expired or
    invalidated."""
    factory: sessionmaker[OrmSession] = session_env.factory
    ttl_us = ttl_minutes * 60 * 1_000_000

    # Draw an "inside the window" offset in [0, ttl) and an "at/after expiry"
    # offset >= 0 (0 lands exactly on the exclusive boundary expires_at).
    inside_us = data.draw(st.integers(min_value=0, max_value=ttl_us - 1))
    after_us = data.draw(st.integers(min_value=0, max_value=2_000_000_000))

    db = factory()
    try:
        issued = issue_session(db, session_env.user_id, ttl_minutes=ttl_minutes, now=t)
        db.commit()
        token = issued.token
        expires_at = issued.expires_at

        # Valid at the lower bound (now == issued_at) -- inclusive.
        assert validate_session(db, token, now=t) is not None

        # Valid at any instant strictly inside the window.
        now_inside = t + timedelta(microseconds=inside_us)
        assert validate_session(db, token, now=now_inside) is not None

        # Just before expiry (exclusive upper bound) is still valid.
        assert (
            validate_session(db, token, now=expires_at - timedelta(microseconds=1))
            is not None
        )

        # At exactly expires_at and at any later instant -> invalid.
        assert validate_session(db, token, now=expires_at) is None
        now_after = expires_at + timedelta(microseconds=after_us)
        assert validate_session(db, token, now=now_after) is None

        # After logout/invalidation the token never validates again, even well
        # inside what would have been the live window.
        invalidate_session(db, token)
        db.commit()
        assert validate_session(db, token, now=t) is None
        assert validate_session(db, token, now=now_inside) is None
    finally:
        db.close()


# ===========================================================================
# Shared HTTP environment for Properties 3 & 4
# ===========================================================================

_KNOWN_USERNAME = "alice"
_KNOWN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="module")
def http_env() -> Iterator[SimpleNamespace]:
    """Module-scoped TestClient over the real app plus a protected probe route.

    Seeds a known user (for Property 4's "non-matching" baseline) and two
    *invalid* session rows -- one expired, one invalidated -- for Property 3.
    Crucially it seeds **no currently-valid token**, so any generated/unknown
    bearer token is guaranteed to be rejected. Module-scoped to satisfy
    Hypothesis; the login/probe rejection paths never create sessions, so state
    does not leak across examples.
    """
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)

    # Seed a known user and two invalid sessions belonging to it.
    db = factory()
    user = User(username=_KNOWN_USERNAME, password_hash=hash_password(_KNOWN_PASSWORD))
    db.add(user)
    db.commit()
    user_id = user.id

    past = utcnow() - timedelta(minutes=180)
    expired = SessionModel(
        token="expired-seeded-token",
        user_id=user_id,
        issued_at=past,
        expires_at=past + timedelta(minutes=60),
        invalidated=False,
    )
    invalidated = SessionModel(
        token="invalidated-seeded-token",
        user_id=user_id,
        issued_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=60),
        invalidated=True,
    )
    db.add_all([expired, invalidated])
    db.commit()
    baseline_sessions = db.execute(
        select(func.count()).select_from(SessionModel)
    ).scalar_one()
    db.close()

    test_settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        max_credential_length=256,
        session_ttl_minutes=60,
    )  # type: ignore[call-arg]

    app: FastAPI = create_app()

    @app.get("/api/_probe")
    def _probe(user: CurrentUser) -> dict[str, str]:  # pragma: no cover - trivial
        return {"id": user.id, "username": user.username}

    def _override_get_session() -> Iterator[OrmSession]:
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    def session_count() -> int:
        s = factory()
        try:
            return int(
                s.execute(select(func.count()).select_from(SessionModel)).scalar_one()
            )
        finally:
            s.close()

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            session_count=session_count,
            baseline_sessions=baseline_sessions,
            expired_token=expired.token,
            invalidated_token=invalidated.token,
        )

    app.dependency_overrides.clear()
    engine.dispose()


# ===========================================================================
# Property 3: Protected endpoints reject unauthenticated access
# ===========================================================================

# Malformed Authorization header values: none of these carry a valid bearer
# token (wrong scheme, missing/empty token, junk). Each must yield 401.
_malformed_auth_headers = st.sampled_from(
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer    ",
        "Basic dXNlcjpwYXNz",
        "Token abc123",
        "NotBearer whatever",
        "garbage-without-scheme",
        "bearer",  # scheme only, no token
    ]
)


# Feature: meeting-minutes-service, Property 3: For any non-public endpoint and any request bearing an absent, malformed, expired, or invalidated token, the Backend_API responds with 401 and returns no meeting content.
# Validates: Requirements 1.1
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_property3_protected_endpoint_rejects_unauthenticated(
    http_env: SimpleNamespace, data: st.DataObject
) -> None:
    """Any absent/malformed/unknown/expired/invalidated credential against a
    protected route -> 401 with an empty body (no content)."""
    kind = data.draw(
        st.sampled_from(
            ["absent", "malformed", "unknown", "expired", "invalidated"]
        )
    )

    if kind == "absent":
        headers: dict[str, str] = {}
    elif kind == "malformed":
        headers = {"Authorization": data.draw(_malformed_auth_headers)}
    elif kind == "unknown":
        # A random token that cannot collide with any seeded token (none of
        # which are valid anyway). Prefix guarantees no accidental match.
        rnd = data.draw(
            st.text(
                alphabet=st.characters(min_codepoint=33, max_codepoint=126),
                min_size=1,
                max_size=64,
            )
        )
        headers = {"Authorization": f"Bearer unknown-{rnd}"}
    elif kind == "expired":
        headers = {"Authorization": f"Bearer {http_env.expired_token}"}
    else:  # invalidated
        headers = {"Authorization": f"Bearer {http_env.invalidated_token}"}

    resp = http_env.client.get("/api/_probe", headers=headers)

    assert resp.status_code == 401
    # No meeting content: the body is empty.
    assert resp.content == b""


# ===========================================================================
# Property 4: Login rejects invalid credentials and out-of-bounds fields
# ===========================================================================

# In-bounds (non-empty, <= 256 chars) credential fields for the 401 clause.
_in_bounds_field = st.text(
    alphabet=st.characters(codec="utf-8"), min_size=1, max_size=256
)

# A field that is either out-of-bounds (empty or > 256 chars) or in-bounds, so
# we can generate logins where at least one field violates the bounds.
_maybe_oob_field = st.one_of(
    st.just(""),  # empty -> out of bounds
    st.text(alphabet=st.characters(codec="utf-8"), min_size=257, max_size=320),
    st.text(alphabet=st.characters(codec="utf-8"), min_size=1, max_size=256),
)


def _is_out_of_bounds(value: str) -> bool:
    return len(value) == 0 or len(value) > 256


# Feature: meeting-minutes-service, Property 4: For any credential pair that does not match a stored user, login is rejected with 401 and no session is created; and for any login request whose username or password field is empty or exceeds 256 characters, login is rejected with 400 and no session is created.
# Validates: Requirements 1.3, 1.4
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    bad_username=_in_bounds_field,
    bad_password=_in_bounds_field,
    oob_username=_maybe_oob_field,
    oob_password=_maybe_oob_field,
)
def test_property4_login_rejects_invalid_and_out_of_bounds(
    http_env: SimpleNamespace,
    bad_username: str,
    bad_password: str,
    oob_username: str,
    oob_password: str,
) -> None:
    """Non-matching credentials -> 401, out-of-bounds fields -> 400, and neither
    ever creates a session."""
    baseline = http_env.baseline_sessions

    # --- Clause 1: in-bounds but non-matching credentials -> 401, no session.
    # Exclude the single valid pair; everything else does not match a stored
    # user (unknown user, or known user with the wrong password).
    assume((bad_username, bad_password) != (_KNOWN_USERNAME, _KNOWN_PASSWORD))
    resp401 = http_env.client.post(
        "/api/auth/login",
        json={"username": bad_username, "password": bad_password},
    )
    assert resp401.status_code == 401
    assert "token" not in resp401.json()
    assert http_env.session_count() == baseline

    # --- Clause 2: at least one out-of-bounds field -> 400, no session.
    assume(_is_out_of_bounds(oob_username) or _is_out_of_bounds(oob_password))
    resp400 = http_env.client.post(
        "/api/auth/login",
        json={"username": oob_username, "password": oob_password},
    )
    assert resp400.status_code == 400
    assert http_env.session_count() == baseline
