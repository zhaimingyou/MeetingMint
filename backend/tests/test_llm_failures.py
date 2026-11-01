"""Failure-taxonomy unit tests for HttpLlmClient.generate_minutes (task 6.3).

Task 6.3 finalizes and verifies the complete failure taxonomy at the LLM_Client
level:

* **Transport failures -> :class:`LlmError`** (Req 6.5): the service is
  unreachable (``httpx.ConnectError``), the request times out
  (``httpx.ReadTimeout`` / the ``TimeoutException`` family), the service returns
  an error HTTP status (``raise_for_status`` -> ``httpx.HTTPStatusError``), or the
  transport returns a body that is not a valid JSON *envelope* (``response.json()``
  raises). The last case is treated as a protocol/transport-level malformation
  (the server did not return a JSON envelope at all), so it maps to ``LlmError``
  rather than ``LlmSchemaError``.

* **Non-conforming content -> :class:`LlmSchemaError`** (Req 6.6): a well-formed
  OpenAI envelope is returned, but the assistant *message content* does not
  conform -- it is missing, not a string, not valid JSON, not a JSON object, or
  violates the minutes schema (e.g. ``topics`` is not a list).

* **No partial result**: on every failure path ``generate_minutes`` raises
  before a :class:`MeetingMinutesData` is built, so the worker (task 9) persists
  nothing partial. ``LlmSchemaError`` subclasses ``LlmError`` so the worker can
  catch ``LlmError`` broadly to fail the job.

The httpx transport is injected via ``httpx.MockTransport`` (monkeypatching
``httpx.Client``), mirroring ``tests/test_llm_generate_minutes.py`` so no real
LLM_Service is contacted. The hypothesis property test (Property 16) is task 6.4.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.base import LlmError, LlmSchemaError
from app.clients.http import HttpLlmClient
from app.clients.types import TranscriptInput
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers (mirrors tests/test_llm_generate_minutes.py)
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


def _envelope(content: str) -> dict:
    """Wrap a raw assistant ``content`` string as an OpenAI chat envelope."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


_TRANSCRIPT: TranscriptInput = {
    "segments": [
        {"start": 0.0, "end": 2.0, "text": "我们开始开会吧", "speaker": None},
    ],
    "language": "zh",
}


def _client() -> HttpLlmClient:
    return HttpLlmClient(_settings())


# --------------------------------------------------------------------------- #
# LlmSchemaError is a subclass of LlmError (worker catches LlmError broadly)
# --------------------------------------------------------------------------- #
def test_schema_error_is_subclass_of_llm_error():
    assert issubclass(LlmSchemaError, LlmError)


# --------------------------------------------------------------------------- #
# Transport failures -> LlmError (Req 6.5)
# --------------------------------------------------------------------------- #
def test_connect_error_maps_to_llm_error(monkeypatch):
    """Unreachable service (ConnectError) -> LlmError, nothing returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmError) as exc_info:
        _client().generate_minutes(_TRANSCRIPT)
    # A pure transport failure must not be reported as a schema error.
    assert not isinstance(exc_info.value, LlmSchemaError)


@pytest.mark.parametrize(
    "timeout_exc",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.TimeoutException,
    ],
)
def test_timeout_family_maps_to_llm_error(monkeypatch, timeout_exc):
    """The whole timeout family -> LlmError (Req 6.1/6.5).

    Covers the per-request ``llm_request_timeout_seconds`` budget: connect, read,
    write, and pool timeouts (all ``httpx.TimeoutException`` subclasses) plus the
    base ``TimeoutException`` itself must map to ``LlmError``, never a schema error.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise timeout_exc("timed out", request=request)

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmError) as exc_info:
        _client().generate_minutes(_TRANSCRIPT)
    assert not isinstance(exc_info.value, LlmSchemaError)


def test_other_http_error_maps_to_llm_error(monkeypatch):
    """Any other ``httpx.HTTPError`` (non-timeout transport error) -> LlmError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmError) as exc_info:
        _client().generate_minutes(_TRANSCRIPT)
    assert not isinstance(exc_info.value, LlmSchemaError)


@pytest.mark.parametrize("status", [500, 503, 502, 429])
def test_error_status_maps_to_llm_error(monkeypatch, status):
    """An error HTTP status (raise_for_status) -> LlmError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "boom"})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmError) as exc_info:
        _client().generate_minutes(_TRANSCRIPT)
    assert not isinstance(exc_info.value, LlmSchemaError)


def test_non_json_envelope_body_maps_to_llm_error(monkeypatch):
    """A 200 whose *envelope* body is not JSON -> LlmError (documented decision).

    The transport succeeded but the server did not return a JSON envelope at all;
    this is a protocol/transport-level malformation, distinct from a schema-invalid
    assistant message (which would be an LlmSchemaError).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmError) as exc_info:
        _client().generate_minutes(_TRANSCRIPT)
    assert not isinstance(exc_info.value, LlmSchemaError)


def test_missing_url_or_model_maps_to_llm_error():
    """No endpoint URL / model configured -> LlmError (defensive guard)."""
    settings = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        llm_impl="mock",  # mock mode allows a missing endpoint URL
        llm_endpoint_url=None,
        llm_model_name=None,
    )  # type: ignore[call-arg]
    client = HttpLlmClient(settings)
    with pytest.raises(LlmError):
        client.generate_minutes(_TRANSCRIPT)


# --------------------------------------------------------------------------- #
# Non-conforming content -> LlmSchemaError (Req 6.6)
# --------------------------------------------------------------------------- #
def test_missing_message_content_maps_to_schema_error(monkeypatch):
    """A well-formed envelope lacking choices[0].message.content -> LlmSchemaError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmSchemaError):
        _client().generate_minutes(_TRANSCRIPT)


def test_non_string_content_maps_to_schema_error(monkeypatch):
    """content that is not a string -> LlmSchemaError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": {"not": "a string"}}}]}
        )

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmSchemaError):
        _client().generate_minutes(_TRANSCRIPT)


def test_content_not_valid_json_maps_to_schema_error(monkeypatch):
    """Assistant message content that is not valid JSON -> LlmSchemaError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope("这并不是合法的 JSON"))

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmSchemaError):
        _client().generate_minutes(_TRANSCRIPT)


def test_content_not_json_object_maps_to_schema_error(monkeypatch):
    """Content that decodes to a non-object (e.g. a list) -> LlmSchemaError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(json.dumps(["not", "an", "object"])))

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmSchemaError):
        _client().generate_minutes(_TRANSCRIPT)


def test_schema_violation_topics_not_a_list_maps_to_schema_error(monkeypatch):
    """A JSON object whose 'topics' is not a list -> LlmSchemaError."""
    bad = {
        "summary": "摘要",
        "topics": "这应该是个数组",  # schema violation: not a list
        "decisions": [],
        "action_items": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(json.dumps(bad)))

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(LlmSchemaError):
        _client().generate_minutes(_TRANSCRIPT)


# --------------------------------------------------------------------------- #
# No partial result: a failure never yields a (partial) MeetingMinutesData
# --------------------------------------------------------------------------- #
def test_failure_returns_no_partial_minutes(monkeypatch):
    """On a schema failure, generate_minutes raises rather than returning data.

    Verifies the no-partial-result guarantee at the client level: there is no
    code path that swallows a schema error mid-parse and returns a half-built
    MeetingMinutesData.
    """
    # Valid summary but an invalid decisions field: a partial-friendly parser
    # might be tempted to return the summary, but the contract is all-or-nothing.
    bad = {
        "summary": "已经有了摘要",
        "topics": ["议题一"],
        "decisions": {"not": "a list"},  # schema violation
        "action_items": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_envelope(json.dumps(bad)))

    _install_mock_transport(monkeypatch, handler)

    result = None
    with pytest.raises(LlmSchemaError):
        result = _client().generate_minutes(_TRANSCRIPT)
    assert result is None
