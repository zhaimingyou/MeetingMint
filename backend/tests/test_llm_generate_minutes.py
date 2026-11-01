"""Example/edge unit tests for HttpLlmClient.generate_minutes transport (task 6.1).

These focus on the request/response transport half that task 6.1 owns:

* a well-formed OpenAI-compatible response (``choices[0].message.content`` = a
  JSON string of the minutes) is POSTed to ``endpoint_url`` and parsed into
  :class:`MeetingMinutesData`, with the request body carrying ``model`` ==
  ``model_name`` and a system+user ``messages`` array (Req 6.1),
* the request uses the configured timeout (``httpx.Timeout(request_timeout)``),
* the rendered user prompt includes each segment's text and its speaker label
  when present.

The httpx transport is injected via ``httpx.MockTransport`` (monkeypatching
``httpx.Client``), mirroring ``tests/test_asr_transcribe.py`` so no real
LLM_Service is contacted. Robust schema validation / empty-list / owner
defaulting is task 6.2; the failure taxonomy is task 6.3; the hypothesis
property tests (Properties 14/15/16) are task 6.4.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.base import LlmError
from app.clients.http import LLM_SYSTEM_PROMPT, HttpLlmClient
from app.clients.types import TranscriptInput
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings(
    *,
    endpoint_url: str = "http://llm.internal:8000/v1/chat/completions",
    model_name: str = "qwen2.5:7b",
    request_timeout_seconds: int = 120,
) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        llm_impl="http",
        llm_endpoint_url=endpoint_url,
        llm_model_name=model_name,
        llm_request_timeout_seconds=request_timeout_seconds,
    )  # type: ignore[call-arg]


def _install_mock_transport(monkeypatch, handler) -> None:
    """Route httpx.Client through a MockTransport running ``handler``."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)


def _minutes_payload(content: dict) -> dict:
    """Wrap minutes dict as an OpenAI-compatible chat completion response."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps(content)}}
        ]
    }


# --------------------------------------------------------------------------- #
# Happy path: POST shape + parse
# --------------------------------------------------------------------------- #
def test_posts_to_endpoint_with_model_and_messages_and_parses(monkeypatch):
    captured: dict = {}

    minutes = {
        "summary": "本次会议讨论了项目排期。",
        "topics": ["排期", "预算"],
        "decisions": ["按计划推进"],
        "action_items": [{"description": "完成对接", "owner": "张伟"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_minutes_payload(minutes))

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(_settings())
    transcript: TranscriptInput = {
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "我们开始吧", "speaker": None},
        ],
        "language": "zh",
    }
    result = client.generate_minutes(transcript)

    # Request transport contract.
    assert captured["method"] == "POST"
    assert captured["url"] == "http://llm.internal:8000/v1/chat/completions"
    body = captured["body"]
    assert body["model"] == "qwen2.5:7b"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["messages"][0]["content"] == LLM_SYSTEM_PROMPT

    # Parsed minutes.
    assert result["summary"] == "本次会议讨论了项目排期。"
    assert result["topics"] == ["排期", "预算"]
    assert result["decisions"] == ["按计划推进"]
    assert result["action_items"] == [{"description": "完成对接", "owner": "张伟"}]


# --------------------------------------------------------------------------- #
# Timeout: request uses the configured httpx.Timeout
# --------------------------------------------------------------------------- #
def test_request_uses_configured_timeout(monkeypatch):
    captured: dict = {}

    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_minutes_payload(
                {"summary": "s", "topics": [], "decisions": [], "action_items": []}
            ),
        )

    transport = httpx.MockTransport(handler)

    def _client_factory(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)

    client = HttpLlmClient(_settings(request_timeout_seconds=37))
    client.generate_minutes({"segments": []})

    assert captured["timeout"] == httpx.Timeout(37.0)


# --------------------------------------------------------------------------- #
# Prompt rendering: transcript text + speaker labels reach the model
# --------------------------------------------------------------------------- #
def test_user_prompt_includes_segment_text_and_speaker(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_minutes_payload(
                {"summary": "s", "topics": [], "decisions": [], "action_items": []}
            ),
        )

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(_settings())
    transcript: TranscriptInput = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "大家好", "speaker": "发言人A"},
            {"start": 1.0, "end": 2.0, "text": "开始开会", "speaker": None},
        ],
    }
    client.generate_minutes(transcript)

    user_content = captured["body"]["messages"][1]["content"]
    assert "发言人A: 大家好" in user_content  # speaker prefixed when present
    assert "开始开会" in user_content  # plain text when no speaker


# --------------------------------------------------------------------------- #
# Transport failure scaffolding -> LlmError (full taxonomy is task 6.3)
# --------------------------------------------------------------------------- #
def test_transport_error_raises_llm_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(_settings())
    with pytest.raises(LlmError):
        client.generate_minutes({"segments": []})
