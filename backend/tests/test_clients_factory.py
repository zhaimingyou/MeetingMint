"""Unit tests for the ASR/LLM client factories (task 4.3).

Example-based tests confirming that ``make_asr_client`` / ``make_llm_client``:
  * return the mock implementation when the ``*_impl`` selector is ``"mock"``,
  * return the HTTP skeleton when the selector is ``"http"``,
  * return objects that structurally satisfy the ``AsrClient`` / ``LlmClient``
    Protocols,
  * default to ``get_settings()`` when called with no/None settings,
  * propagate the configured HTTP transport fields onto the HTTP clients.

The hypothesis property test for factory selection (Property 28) is task 4.4;
these are the example tests for the factory wiring (task 4.3). Method behavior
of the HTTP skeletons (``transcribe`` / ``generate_minutes``) is owned by tasks
5/6, so it is only lightly asserted here (currently raises NotImplementedError)
in a way that documents the boundary without coupling to future logic.
"""

from __future__ import annotations

import pytest

from app.clients import (
    AsrClient,
    HttpAsrClient,
    HttpLlmClient,
    LlmClient,
    MockAsrClient,
    MockLlmClient,
    make_asr_client,
    make_llm_client,
)
from app.config import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    """Build a Settings with required fields satisfied and no .env/file reads."""
    base: dict[str, object] = {
        "bootstrap_admin_username": "admin",
        "bootstrap_admin_password": "bootstrap-pass",
        "redis_url": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ASR factory selection
# --------------------------------------------------------------------------- #
def test_make_asr_client_returns_mock_when_mock() -> None:
    client = make_asr_client(_settings(asr_impl="mock"))
    assert isinstance(client, MockAsrClient)
    assert isinstance(client, AsrClient)


def test_make_asr_client_returns_http_when_http() -> None:
    client = make_asr_client(
        _settings(
            asr_impl="http",
            asr_endpoint_url="http://192.0.2.20:9000/asr",
        )
    )
    assert isinstance(client, HttpAsrClient)
    assert isinstance(client, AsrClient)


def test_make_asr_client_propagates_http_config() -> None:
    client = make_asr_client(
        _settings(
            asr_impl="http",
            asr_endpoint_url="http://192.0.2.20:9000/asr",
            asr_request_timeout_seconds=90,
            asr_chunk_length_seconds=120,
            asr_max_retries=3,
        )
    )
    assert isinstance(client, HttpAsrClient)
    assert client.endpoint_url == "http://192.0.2.20:9000/asr"
    assert client.request_timeout_seconds == 90
    assert client.chunk_length_seconds == 120
    assert client.max_retries == 3


# --------------------------------------------------------------------------- #
# LLM factory selection
# --------------------------------------------------------------------------- #
def test_make_llm_client_returns_mock_when_mock() -> None:
    client = make_llm_client(_settings(llm_impl="mock"))
    assert isinstance(client, MockLlmClient)
    assert isinstance(client, LlmClient)


def test_make_llm_client_returns_http_when_http() -> None:
    client = make_llm_client(
        _settings(
            llm_impl="http",
            llm_endpoint_url="http://192.0.2.20:8000/v1/chat/completions",
            llm_model_name="qwen2.5",
        )
    )
    assert isinstance(client, HttpLlmClient)
    assert isinstance(client, LlmClient)


def test_make_llm_client_propagates_http_config() -> None:
    client = make_llm_client(
        _settings(
            llm_impl="http",
            llm_endpoint_url="http://192.0.2.20:8000/v1/chat/completions",
            llm_model_name="qwen2.5",
            llm_request_timeout_seconds=75,
        )
    )
    assert isinstance(client, HttpLlmClient)
    assert client.endpoint_url == "http://192.0.2.20:8000/v1/chat/completions"
    assert client.model_name == "qwen2.5"
    assert client.request_timeout_seconds == 75


# --------------------------------------------------------------------------- #
# Default-to-get_settings convenience
# --------------------------------------------------------------------------- #
def test_factories_default_to_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-pass")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("ASR_IMPL", "mock")
    monkeypatch.setenv("LLM_IMPL", "mock")
    get_settings.cache_clear()
    try:
        assert isinstance(make_asr_client(), MockAsrClient)
        assert isinstance(make_llm_client(), MockLlmClient)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# HTTP boundary: construction works and the transport methods are implemented.
# The ASR method (task 5.2) orchestrates via injectable seams; the LLM method
# (task 6.1) issues the OpenAI-compatible chat call -- neither raises
# NotImplementedError any longer.
# --------------------------------------------------------------------------- #
def test_http_asr_construction_and_transcribe_implemented() -> None:
    client = make_asr_client(
        _settings(asr_impl="http", asr_endpoint_url="http://host:9000/asr")
    )
    assert isinstance(client, HttpAsrClient)
    # transcribe is implemented (task 5.2). With injected seams it orchestrates
    # chunking + assembly without touching the network or ffprobe.
    client._duration_prober = lambda _path: 5.0  # type: ignore[assignment]
    client._chunk_transcriber = lambda _p, _s, _e: [  # type: ignore[assignment]
        {"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}
    ]
    assert client.transcribe("/tmp/whatever.wav") == [
        {"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}
    ]


def test_http_llm_construction_and_generate_minutes_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx

    client = make_llm_client(
        _settings(
            llm_impl="http",
            llm_endpoint_url="http://host:8000/v1/chat/completions",
            llm_model_name="qwen2.5",
        )
    )
    assert isinstance(client, HttpLlmClient)

    # generate_minutes is implemented (task 6.1). Route httpx through a mock
    # transport so the OpenAI-compatible call is exercised without a network.
    minutes = {"summary": "纪要", "topics": [], "decisions": [], "action_items": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(minutes)}}]},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)

    assert client.generate_minutes({"segments": []}) == minutes
