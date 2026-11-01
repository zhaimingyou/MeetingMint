"""Tests for the real-model adaptation of the HTTP ASR/LLM clients.

Covers the wiring added so the framework talks to the actually-provisioned
models without code changes beyond configuration:

* **LLM (DeepSeek / OpenAI-compatible).** The chat-completions URL is resolved
  from either a base URL (``https://api.deepseek.com`` / ``.../v1``) or a full
  URL; a bearer token is attached as ``Authorization: Bearer <key>`` only when
  ``llm_api_key`` is configured; and code-fence-wrapped JSON content still parses.
* **ASR (DGX Spark FunASR / SenseVoice).** The transcriptions URL is resolved
  from a base endpoint to ``{base}/v1/audio/transcriptions``.

All transport is exercised through ``httpx.MockTransport`` (no network), and the
ASR audio slicer is injected so no real ffmpeg / audio file is needed.
"""

from __future__ import annotations

import json

import httpx

from app.clients.http import (
    HttpAsrClient,
    HttpLlmClient,
    _chat_completions_url,
    _transcriptions_url,
)
from app.config import Settings

# A minimal schema-valid minutes object.
_MINUTES = {
    "summary": "本次会议讨论了项目进度。",
    "topics": ["进度回顾"],
    "decisions": [],
    "action_items": [],
}


def _install_mock_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)


def _llm_settings(*, endpoint_url: str, api_key: str | None = None) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        llm_impl="http",
        llm_endpoint_url=endpoint_url,
        llm_model_name="deepseek-chat",
        llm_api_key=api_key,
    )  # type: ignore[call-arg]


def _asr_settings(*, endpoint_url: str) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="http",
        asr_endpoint_url=endpoint_url,
        asr_model_name="sensevoice",
    )  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# URL resolution helpers
# --------------------------------------------------------------------------- #
def test_chat_completions_url_resolution():
    assert (
        _chat_completions_url("https://api.deepseek.com")
        == "https://api.deepseek.com/chat/completions"
    )
    assert (
        _chat_completions_url("https://api.deepseek.com/v1")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    # Trailing slash tolerated.
    assert (
        _chat_completions_url("https://api.deepseek.com/v1/")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    # A full URL is used as-is.
    assert (
        _chat_completions_url("http://llm.local:8000/v1/chat/completions")
        == "http://llm.local:8000/v1/chat/completions"
    )


def test_transcriptions_url_resolution():
    assert (
        _transcriptions_url("http://192.0.2.20:19100")
        == "http://192.0.2.20:19100/v1/audio/transcriptions"
    )
    assert (
        _transcriptions_url("http://192.0.2.20:19100/v1")
        == "http://192.0.2.20:19100/v1/audio/transcriptions"
    )
    assert (
        _transcriptions_url("http://h:19100/")
        == "http://h:19100/v1/audio/transcriptions"
    )
    # A full URL is used as-is.
    full = "http://h:19100/v1/audio/transcriptions"
    assert _transcriptions_url(full) == full


# --------------------------------------------------------------------------- #
# LLM: DeepSeek base URL + bearer auth
# --------------------------------------------------------------------------- #
def test_llm_base_url_normalized_and_bearer_attached(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_MINUTES)}}]}
        )

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(
        _llm_settings(endpoint_url="https://api.deepseek.com", api_key="sk-test-123")
    )
    result = client.generate_minutes({"segments": []})

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["auth"] == "Bearer sk-test-123"
    assert result["summary"] == _MINUTES["summary"]


def test_llm_no_api_key_sends_no_auth_header(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(_MINUTES)}}]}
        )

    _install_mock_transport(monkeypatch, handler)

    # Local unauthenticated LLM, base URL ending in /v1.
    client = HttpLlmClient(_llm_settings(endpoint_url="http://llm.local:8000/v1"))
    client.generate_minutes({"segments": []})

    assert captured["url"] == "http://llm.local:8000/v1/chat/completions"
    assert captured["auth"] is None


# --------------------------------------------------------------------------- #
# LLM: code-fence-wrapped JSON still parses
# --------------------------------------------------------------------------- #
def test_llm_parses_code_fenced_json(monkeypatch):
    fenced = "```json\n" + json.dumps(_MINUTES) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(_llm_settings(endpoint_url="https://api.deepseek.com/v1"))
    result = client.generate_minutes({"segments": []})

    assert result["summary"] == _MINUTES["summary"]
    assert result["topics"] == _MINUTES["topics"]


# --------------------------------------------------------------------------- #
# ASR: base endpoint normalized to /v1/audio/transcriptions; model + format sent
# --------------------------------------------------------------------------- #
def test_asr_posts_to_transcriptions_url_with_model_and_format(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content"] = request.content
        return httpx.Response(
            200,
            json={
                "language": "zh",
                "duration": 1.0,
                "text": "你好",
                "segments": [{"start": 0.0, "end": 1.0, "text": "你好"}],
            },
        )

    _install_mock_transport(monkeypatch, handler)

    client = HttpAsrClient(
        _asr_settings(endpoint_url="http://192.0.2.20:19100"),
        duration_prober=lambda _p: 3.0,
        audio_slicer=lambda path, _s, _e: path,  # no real ffmpeg
    )
    segments = client.transcribe(str(audio))

    assert captured["url"] == "http://192.0.2.20:19100/v1/audio/transcriptions"
    body = captured["content"]
    assert b'name="file"' in body
    assert b"sensevoice" in body
    assert b"verbose_json" in body
    # SenseVoice omits speaker -> mapped to None.
    assert segments == [{"start": 0.0, "end": 1.0, "text": "你好", "speaker": None}]
