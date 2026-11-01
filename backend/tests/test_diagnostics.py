"""Tests for the real-model connectivity self-check (app.diagnostics).

All HTTP is exercised through ``httpx.MockTransport`` (no network); the probes
are pure functions of the configured ``Settings`` plus the mocked response.
"""

from __future__ import annotations

import httpx

from app import diagnostics
from app.config import Settings


def _install_mock_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)


def _settings(**overrides) -> Settings:
    base: dict = {
        "bootstrap_admin_username": "admin",
        "bootstrap_admin_password": "bootstrap-pass",
        "redis_url": "redis://localhost:6379/0",
        "database_url": "sqlite://",
        # default both impls to mock; tests flip the one under test to http.
        "asr_impl": "mock",
        "llm_impl": "mock",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# models_url resolution
# --------------------------------------------------------------------------- #
def test_models_url_resolution():
    assert diagnostics.models_url("https://api.deepseek.com") == "https://api.deepseek.com/models"
    assert diagnostics.models_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/models"
    assert (
        diagnostics.models_url("http://host:8000/v1/chat/completions")
        == "http://host:8000/v1/models"
    )
    assert diagnostics.models_url("https://api.deepseek.com/") == "https://api.deepseek.com/models"


# --------------------------------------------------------------------------- #
# ASR probe
# --------------------------------------------------------------------------- #
def test_asr_skipped_when_mock():
    status, _ = diagnostics.check_asr(_settings(asr_impl="mock"))
    assert status == "skip"


def test_asr_ok_when_health_lists_model(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"status": "ok", "device": "cuda:0", "models_loaded": ["sensevoice"]}
        )

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_asr(
        _settings(asr_impl="http", asr_endpoint_url="http://192.0.2.20:19100", asr_model_name="sensevoice")
    )
    assert status == "ok"
    # Health is probed at the origin root, not under /v1.
    assert captured["url"] == "http://192.0.2.20:19100/health"
    assert "sensevoice" in msg


def test_asr_warns_when_model_not_loaded(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "models_loaded": ["paraformer"]})

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_asr(
        _settings(asr_impl="http", asr_endpoint_url="http://asr:19100", asr_model_name="sensevoice")
    )
    assert status == "warn"
    assert "not loaded" in msg


def test_asr_fails_when_unreachable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_asr(
        _settings(asr_impl="http", asr_endpoint_url="http://asr:19100")
    )
    assert status == "fail"
    assert "unreachable" in msg


# --------------------------------------------------------------------------- #
# LLM probe
# --------------------------------------------------------------------------- #
def test_llm_skipped_when_mock():
    status, _ = diagnostics.check_llm(_settings(llm_impl="mock"))
    assert status == "skip"


def test_llm_ok_with_auth_and_model_listed(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]},
        )

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_llm(
        _settings(
            llm_impl="http",
            llm_endpoint_url="https://api.deepseek.com",
            llm_model_name="deepseek-chat",
            llm_api_key="sk-test-123",
        )
    )
    assert status == "ok"
    assert captured["url"] == "https://api.deepseek.com/models"
    assert captured["auth"] == "Bearer sk-test-123"


def test_llm_fails_on_401(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_llm(
        _settings(
            llm_impl="http",
            llm_endpoint_url="https://api.deepseek.com",
            llm_model_name="deepseek-chat",
            llm_api_key="sk-bad",
        )
    )
    assert status == "fail"


def test_llm_warns_when_model_not_listed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-reasoner"}]})

    _install_mock_transport(monkeypatch, handler)
    status, msg = diagnostics.check_llm(
        _settings(
            llm_impl="http",
            llm_endpoint_url="https://api.deepseek.com/v1",
            llm_model_name="deepseek-chat",
            llm_api_key="sk-test",
        )
    )
    assert status == "warn"
    assert "not listed" in msg


# --------------------------------------------------------------------------- #
# main() exit codes
# --------------------------------------------------------------------------- #
def test_main_returns_zero_when_all_mock(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "get_settings", lambda: _settings())
    assert diagnostics.main([]) == 0
    out = capsys.readouterr().out
    assert "SKIP" in out


def test_main_returns_one_when_a_probe_fails(monkeypatch):
    monkeypatch.setattr(diagnostics, "get_settings", lambda: _settings())
    monkeypatch.setattr(diagnostics, "check_llm", lambda _s: ("fail", "boom"))
    assert diagnostics.main([]) == 1
