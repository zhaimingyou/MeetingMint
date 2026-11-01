"""Integration tests for the health-check endpoint -- task 12.2.

Covers ``GET /api/health`` (task 12.1): ready when both required dependencies
(SQLite + Redis) are up (Req 12.7), and not-ready when either is down (Req 12.8).
ASR/LLM are deliberately excluded from the probe. The endpoint is public (no
auth), so an orchestrator probe reaches it without a token.

The two dependency probes are module-level seams in :mod:`app.api.health`
(``check_database`` / ``check_redis``); these tests monkeypatch them to simulate
a dependency being up or down without needing a real Redis (or a forcibly broken
SQLite) in the environment. One test exercises the *real* ``check_redis`` against
an unreachable port to confirm it fails fast as down.
"""

from __future__ import annotations

import time

import app.api.health as health_mod
from app.config import Settings


def test_health_ready_when_all_dependencies_up(client, monkeypatch) -> None:
    """Both SQLite (real in-memory DB) and Redis up -> 200 + status ready."""
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: True)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["dependencies"] == {"database": "up", "redis": "up"}


def test_health_not_ready_when_redis_down(client, monkeypatch) -> None:
    """Redis down -> 503 + not_ready, with redis reported down (Req 12.8)."""
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: False)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["redis"] == "down"
    assert body["dependencies"]["database"] == "up"


def test_health_not_ready_when_database_down(client, monkeypatch) -> None:
    """SQLite down -> 503 + not_ready, with database reported down (Req 12.8)."""
    monkeypatch.setattr(health_mod, "check_database", lambda db: False)
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: True)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["database"] == "down"


def test_health_not_ready_when_both_down(client, monkeypatch) -> None:
    """Both dependencies down -> 503 + not_ready, both reported down."""
    monkeypatch.setattr(health_mod, "check_database", lambda db: False)
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: False)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"] == {"database": "down", "redis": "down"}


def test_health_requires_no_authentication(client, monkeypatch) -> None:
    """Health is public: reachable with no Authorization header (not 401)."""
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: True)
    resp = client.get("/api/health")  # no auth header
    assert resp.status_code == 200


def test_health_excludes_asr_and_llm(client, monkeypatch) -> None:
    """Only SQLite + Redis are probed; ASR/LLM never appear in the verdict."""
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: True)
    body = client.get("/api/health").json()
    assert set(body["dependencies"]) == {"database", "redis"}


def test_health_responds_within_two_seconds_when_ready(client, monkeypatch) -> None:
    """While dependencies are available the response is within 2s (Req 12.7)."""
    monkeypatch.setattr(health_mod, "check_redis", lambda settings: True)
    start = time.perf_counter()
    resp = client.get("/api/health")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed < 2.0


def test_check_redis_returns_false_when_unreachable() -> None:
    """The real Redis probe fails fast (within the budget) when nothing listens.

    Points at a loopback port with no listener so the connect is refused
    promptly; the probe must report down (False) rather than raise or hang.
    """
    settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://127.0.0.1:6399/0",
        database_url="sqlite://",
    )  # type: ignore[call-arg]
    start = time.perf_counter()
    assert health_mod.check_redis(settings) is False
    assert time.perf_counter() - start < 2.0
