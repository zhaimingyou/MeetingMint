"""Example/edge unit tests for HttpLlmClient.generate_minutes transport (task 6.1).

These focus on the request/transport half that task 6.1 owns:

* the OpenAI-compatible Chat Completions **POST** is issued to
  ``llm_endpoint_url`` (Req 6.1),
* the request **body** carries ``model == llm_model_name``, a Chinese system
  message instructing strict JSON, and the rendered transcript (with speaker
  labels when present) as the user message (Req 6.1),
* the per-request **timeout** is applied via ``httpx.Timeout`` with the
  configured ``llm_request_timeout_seconds`` (Req 6.1),
* a fresh ``httpx.Client`` is created per call (no connection at construction),
* the assistant ``choices[0].message.content`` is parsed into a
  :class:`MeetingMinutesData` (the parse/validate hardening lives in task 6.2;
  these tests just confirm the wiring end to end).

The transport is exercised with ``httpx.MockTransport`` (mirroring
``tests/test_asr_transcribe.py``); no real LLM_Service is contacted. The
hypothesis property tests (Properties 14/15/16) are task 6.4.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.http import LLM_SYSTEM_PROMPT, HttpLlmClient
from app.config import Settings
from app.enums import UNASSIGNED_OWNER

# A representative well-formed assistant payload (Chinese minutes as strict JSON).
_MINUTES_JSON = {
    "summary": "本次会议讨论了项目进度。",
    "topics": ["进度回顾", "下一步计划"],
    "decisions": ["采用方案A"],
    "action_items": [
        {"description": "整理需求文档", "owner": "张三"},
        {"description": "搭建测试环境"},  # missing owner -> defaults
    ],
}


def _settings(
    *,
    endpoint_url: str = "http://llm.internal:8000/v1/chat/completions",
    model_name: str = "qwen2.5:14b",
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


def _install_mock_transport(monkeypatch, handler, captured):
    """Route httpx.Client through a MockTransport and capture constructor kwargs.

    ``captured`` is populated with the ``timeout`` passed to ``httpx.Client`` so
    tests can assert the per-request timeout budget (Req 6.1).
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)


# --------------------------------------------------------------------------- #
# Happy path: request shape + timeout + content parsing
# --------------------------------------------------------------------------- #
def test_generate_minutes_posts_openai_chat_request(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(_MINUTES_JSON)}}
                ]
            },
        )

    _install_mock_transport(monkeypatch, handler, captured)

    client = HttpLlmClient(_settings())
    transcript = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "大家好", "speaker": "发言人A"},
            {"start": 1.0, "end": 2.0, "text": "我们开始吧", "speaker": "发言人B"},
        ],
        "language": "zh",
    }

    result = client.generate_minutes(transcript)

    # POST to the configured endpoint URL (Req 6.1).
    assert captured["method"] == "POST"
    assert captured["url"] == "http://llm.internal:8000/v1/chat/completions"

    body = captured["body"]
    # model == llm_model_name (Req 6.1).
    assert body["model"] == "qwen2.5:14b"

    # Chinese system message instructing strict JSON; user message carries the
    # rendered transcript with speaker labels.
    messages = body["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    user_msg = next(m for m in messages if m["role"] == "user")
    assert system_msg["content"] == LLM_SYSTEM_PROMPT
    assert "JSON" in system_msg["content"]
    assert "中文" in system_msg["content"]
    assert "发言人A: 大家好" in user_msg["content"]
    assert "发言人B: 我们开始吧" in user_msg["content"]

    # Assistant content parsed into MeetingMinutesData (Req 6.2 wiring).
    assert result["summary"] == "本次会议讨论了项目进度。"
    assert result["topics"] == ["进度回顾", "下一步计划"]
    assert result["decisions"] == ["采用方案A"]
    assert result["action_items"] == [
        {"description": "整理需求文档", "owner": "张三"},
        {"description": "搭建测试环境", "owner": UNASSIGNED_OWNER},
    ]


def test_generate_minutes_applies_request_timeout(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_MINUTES_JSON)}}]},
        )

    _install_mock_transport(monkeypatch, handler, captured)

    client = HttpLlmClient(_settings(request_timeout_seconds=37))
    client.generate_minutes({"segments": []})

    # A single httpx.Timeout carrying the configured budget on every channel
    # (connect/read/write/pool), per Req 6.1.
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 37.0
    assert timeout.connect == 37.0
    assert timeout.write == 37.0
    assert timeout.pool == 37.0


def test_user_message_omits_speaker_prefix_when_absent(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_MINUTES_JSON)}}]},
        )

    _install_mock_transport(monkeypatch, handler, captured)

    client = HttpLlmClient(_settings())
    client.generate_minutes(
        {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "无说话人", "speaker": None},
                {"start": 1.0, "end": 2.0, "text": "也没有键"},
            ]
        }
    )

    user_msg = next(m for m in captured["body"]["messages"] if m["role"] == "user")
    # No speaker prefix => the bare text appears, with no "<label>: " in front.
    assert user_msg["content"] == "无说话人\n也没有键"


def test_empty_transcript_still_issues_request(monkeypatch):
    captured: dict = {"called": False}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["called"] = True
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_MINUTES_JSON)}}]},
        )

    _install_mock_transport(monkeypatch, handler, captured)

    client = HttpLlmClient(_settings())
    result = client.generate_minutes({"segments": []})

    assert captured["called"] is True
    user_msg = next(m for m in captured["body"]["messages"] if m["role"] == "user")
    assert user_msg["content"] == ""  # empty transcript renders to ""
    assert result["summary"] == "本次会议讨论了项目进度。"


def test_call_llm_seam_returns_assistant_content(monkeypatch):
    """The _call_llm seam returns the raw assistant string for _parse_minutes."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  raw-assistant-text  "}}]},
        )

    _install_mock_transport(monkeypatch, handler, captured)

    client = HttpLlmClient(_settings())
    content = client._call_llm("一些转写文本")

    assert content == "  raw-assistant-text  "
    user_msg = next(m for m in captured["body"]["messages"] if m["role"] == "user")
    assert user_msg["content"] == "一些转写文本"
