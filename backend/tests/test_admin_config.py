"""Tests for the model configuration API (admin-console, task 7).

Covers Property 3 (invalid change = no-op; valid = full persist), Property 4
(no secret value in any response/audit), range/cross-field validation parity
with startup, reset/omit semantics for secrets, the connectivity test, and admin
gating. Uses a dedicated harness whose base settings use mock implementations
(so the baseline is valid) and carry a CONFIG_SECRET_KEY (so secret writes work).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

import app.api.admin_config as admin_config_mod
from app.config import Settings, get_settings
from app.config_effective import effective_settings_for
from app.db import get_session, make_engine, make_session_factory
from app.main import create_app
from app.models import RuntimeConfigEntry, User, create_all
from app.secrets import decrypt_secret, generate_key
from app.security.passwords import hash_password
from app.security.sessions import issue_session

PASSWORD = "correct-horse-battery-staple"
KEY = generate_key()


def _base_settings() -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="mock",
        llm_impl="mock",
        streaming_asr_impl="mock",
        config_secret_key=KEY,
    )  # type: ignore[call-arg]


@pytest.fixture()
def harness() -> Iterator[tuple[TestClient, sessionmaker[OrmSession], str, str]]:
    engine = make_engine("sqlite://")
    create_all(engine)
    sf = make_session_factory(engine)

    s = sf()
    try:
        admin = User(
            username="admin", password_hash=hash_password(PASSWORD), is_admin=True
        )
        bob = User(username="bob", password_hash=hash_password(PASSWORD), is_admin=False)
        s.add_all([admin, bob])
        s.commit()
        admin_token = issue_session(s, admin.id, ttl_minutes=60).token
        bob_token = issue_session(s, bob.id, ttl_minutes=60).token
        s.commit()
    finally:
        s.close()

    base = _base_settings()
    app = create_app()

    def _override_get_session() -> Iterator[OrmSession]:
        sess = sf()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_settings] = lambda: base

    try:
        with TestClient(app) as client:
            yield client, sf, admin_token, bob_token
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _field(body: dict, key: str) -> dict:
    return next(f for f in body["editable"] if f["key"] == key)


# --- GET ---------------------------------------------------------------------


def test_get_config_lists_editable_and_restart_only(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.get("/api/admin/config", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    model = _field(body, "asr_model_name")
    assert model["value"] == "sensevoice"
    assert model["source"] == "env"
    # Secret field exposes only "configured", never a value.
    secret = _field(body, "llm_api_key")
    assert secret["secret"] is True
    assert secret["configured"] is False
    assert "value" not in secret
    # Restart-only block present and read-only; secret key reported as bool only.
    keys = {f["key"] for f in body["restart_only"]}
    assert "database_url" in keys and "config_secret_key" in keys
    csk = next(f for f in body["restart_only"] if f["key"] == "config_secret_key")
    assert csk["configured"] is True and "value" not in csk


# --- PUT: valid / invalid / atomic (Property 3) ------------------------------


def test_put_valid_non_secret_change_persists(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"asr_model_name": "paraformer"},
    )
    assert resp.status_code == 200
    model = _field(resp.json(), "asr_model_name")
    assert model["value"] == "paraformer"
    assert model["source"] == "runtime"


def test_put_out_of_range_rejected_and_nothing_persisted(harness) -> None:
    client, sf, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"asr_model_name": "paraformer", "asr_max_retries": 999},
    )
    assert resp.status_code == 400
    # Property 3: the whole batch was rejected -- even the valid key in it is not
    # persisted, and the prior config is retained.
    after = client.get("/api/admin/config", headers=_auth(admin_token)).json()
    assert _field(after, "asr_model_name")["value"] == "sensevoice"
    assert _field(after, "asr_max_retries")["source"] == "env"


def test_put_cross_field_http_without_endpoint_rejected(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"llm_impl": "http"},  # missing endpoint URL + model name
    )
    assert resp.status_code == 400


def test_put_unknown_key_rejected(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"database_url": "sqlite:///evil.db"},
    )
    assert resp.status_code == 400


def test_put_http_with_endpoint_and_model_ok(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={
            "llm_impl": "http",
            "llm_endpoint_url": "https://api.deepseek.com",
            "llm_model_name": "deepseek-v4-flash",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert _field(body, "llm_impl")["value"] == "http"
    assert _field(body, "llm_model_name")["value"] == "deepseek-v4-flash"


# --- PUT: secret handling (Property 4) ---------------------------------------


def test_put_secret_is_encrypted_and_never_echoed(harness) -> None:
    client, sf, admin_token, _ = harness
    resp = client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"llm_api_key": "sk-super-secret"},
    )
    assert resp.status_code == 200
    # The secret value appears nowhere in the response body.
    assert "sk-super-secret" not in resp.text
    secret = _field(resp.json(), "llm_api_key")
    assert secret["configured"] is True and "value" not in secret
    # Stored encrypted; decrypts back to the plaintext.
    s = sf()
    try:
        row = s.get(RuntimeConfigEntry, "llm_api_key")
        assert row is not None and row.is_secret is True
        assert row.value != "sk-super-secret"
        assert decrypt_secret(row.value, _base_settings()) == "sk-super-secret"
    finally:
        s.close()


def test_put_omitting_secret_leaves_it_unchanged(harness) -> None:
    client, sf, admin_token, _ = harness
    client.put(
        "/api/admin/config", headers=_auth(admin_token), json={"llm_api_key": "sk-1"}
    )
    s = sf()
    try:
        before = s.get(RuntimeConfigEntry, "llm_api_key").value
    finally:
        s.close()
    # A later change that does not mention the secret must not alter it.
    client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"asr_model_name": "paraformer"},
    )
    s = sf()
    try:
        after = s.get(RuntimeConfigEntry, "llm_api_key").value
    finally:
        s.close()
    assert after == before


def test_put_null_clears_secret(harness) -> None:
    client, sf, admin_token, _ = harness
    client.put(
        "/api/admin/config", headers=_auth(admin_token), json={"llm_api_key": "sk-1"}
    )
    resp = client.put(
        "/api/admin/config", headers=_auth(admin_token), json={"llm_api_key": None}
    )
    assert resp.status_code == 200
    assert _field(resp.json(), "llm_api_key")["configured"] is False
    s = sf()
    try:
        row = s.get(RuntimeConfigEntry, "llm_api_key")
        assert row is not None
        assert row.is_secret is True
        assert row.value == ""
    finally:
        s.close()


def test_put_null_secret_overrides_environment_secret(harness) -> None:
    client, sf, admin_token, _ = harness
    base = _base_settings().model_copy(update={"llm_api_key": "sk-from-env"})
    client.app.dependency_overrides[get_settings] = lambda: base

    resp = client.put(
        "/api/admin/config", headers=_auth(admin_token), json={"llm_api_key": None}
    )

    assert resp.status_code == 200
    secret = _field(resp.json(), "llm_api_key")
    assert secret["source"] == "runtime"
    assert secret["configured"] is False
    s = sf()
    try:
        assert effective_settings_for(base, s).llm_api_key is None
    finally:
        s.close()


def test_put_writes_audit_without_secret_value(harness) -> None:
    client, _, admin_token, _ = harness
    client.put(
        "/api/admin/config",
        headers=_auth(admin_token),
        json={"llm_api_key": "sk-super-secret", "asr_model_name": "paraformer"},
    )
    audit = client.get("/api/admin/audit", headers=_auth(admin_token)).json()
    assert audit["total"] >= 1
    entry = audit["items"][0]
    assert entry["action"] == "config.update"
    assert "sk-super-secret" not in json.dumps(entry)
    assert entry["detail"]["llm_api_key"] == "set"  # marker, not the value
    assert "asr_model_name" in entry["detail"]["keys"]


# --- connectivity test -------------------------------------------------------


def test_connectivity_test_mock_reports_skip(harness) -> None:
    client, _, admin_token, _ = harness
    resp = client.post("/api/admin/config/test", headers=_auth(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["asr"]["status"] == "skip"
    assert body["llm"]["status"] == "skip"


def test_connectivity_test_passes_through_diagnostics(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, admin_token, _ = harness
    monkeypatch.setattr(
        admin_config_mod, "check_asr", lambda s: ("ok", "ASR reachable")
    )
    monkeypatch.setattr(
        admin_config_mod, "check_llm", lambda s: ("warn", "model not listed")
    )
    resp = client.post("/api/admin/config/test", headers=_auth(admin_token))
    body = resp.json()
    assert body["asr"] == {"status": "ok", "message": "ASR reachable"}
    assert body["llm"] == {"status": "warn", "message": "model not listed"}


# --- authz -------------------------------------------------------------------


def test_config_routes_require_admin(harness) -> None:
    client, _, _, bob_token = harness
    assert client.get("/api/admin/config", headers=_auth(bob_token)).status_code == 403
    assert (
        client.put(
            "/api/admin/config", headers=_auth(bob_token), json={"asr_model_name": "x"}
        ).status_code
        == 403
    )
    assert (
        client.post("/api/admin/config/test", headers=_auth(bob_token)).status_code
        == 403
    )


def test_config_routes_require_auth(harness) -> None:
    client, _, _, _ = harness
    assert client.get("/api/admin/config").status_code == 401
    assert client.put("/api/admin/config", json={"asr_model_name": "x"}).status_code == 401
