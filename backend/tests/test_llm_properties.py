"""Property-based tests for the LLM_Client minutes contract (task 6.4).

This module implements the three design LLM properties as ``hypothesis``
property tests (each running >= 100 examples) plus two focused unit tests:

* **Property 14** -- *Conforming LLM output yields complete minutes with
  empty-list defaults* (Req 6.2): for any schema-conforming model output (where
  ``topics`` / ``decisions`` / ``action_items`` may be present lists, explicit
  ``null``, or absent), the produced minutes always carry a ``str`` summary plus
  three lists, and every section with no identified entries is an empty list
  (never ``None`` / missing).
* **Property 15** -- *Every action item has a non-empty owner* (Req 6.3): for any
  generated action items (owners that are real names, absent, ``null``, empty, or
  whitespace), every produced Action_Item has a non-empty ``owner`` string, equal
  to :data:`app.enums.UNASSIGNED_OWNER` exactly when no non-empty owner was
  provided.
* **Property 16** -- *Terminal LLM failure fails the job with no partial minutes*
  (Req 6.5, 6.6): for any transport failure (unreachable / timeout / error
  status / non-JSON envelope) or any non-conforming content, ``generate_minutes``
  *raises* (``LlmError`` for transport, ``LlmSchemaError`` for content) and never
  returns a partial :class:`MeetingMinutesData`.

The pure ``HttpLlmClient._parse_minutes`` static method is exercised directly for
Properties 14/15 (it is the production normalization path). Property 16 drives
the full ``generate_minutes`` transport path with an injected
``httpx.MockTransport`` so no real LLM_Service is contacted. The unit tests cover
request-timeout abort behaviour and the Chinese-in -> Chinese-out guarantee via
the offline :class:`MockLlmClient` (Req 6.4).
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.clients.base import LlmError, LlmSchemaError
from app.clients.http import HttpLlmClient
from app.clients.mock import MockLlmClient
from app.clients.types import TranscriptInput
from app.config import Settings
from app.enums import UNASSIGNED_OWNER


# --------------------------------------------------------------------------- #
# Helpers (mirror tests/test_llm_generate_minutes.py / test_llm_failures.py)
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


def _client() -> HttpLlmClient:
    return HttpLlmClient(_settings())


def _envelope(content: str) -> dict:
    """Wrap a raw assistant ``content`` string as an OpenAI chat envelope."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _install_mock_transport(monkeypatch, handler) -> None:
    """Route httpx.Client through a MockTransport running ``handler`` (unit tests)."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)


@contextlib.contextmanager
def _mock_transport(handler):
    """Per-example httpx.Client patch routing through a MockTransport.

    Used inside ``@given`` property bodies (where the pytest ``monkeypatch``
    fixture cannot vary per generated example): patches ``httpx.Client`` so the
    client's ``with httpx.Client(...)`` block uses the MockTransport ``handler``.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    with mock.patch.object(httpx, "Client", _client_factory):
        yield


_TRANSCRIPT: TranscriptInput = {
    "segments": [
        {"start": 0.0, "end": 2.0, "text": "我们开始开会吧", "speaker": None},
    ],
    "language": "zh",
}


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# --------------------------------------------------------------------------- #
# Strategies for Property 14
# --------------------------------------------------------------------------- #
# A conforming string-list entry is any scalar the parser coerces to ``str``.
# Strings (incl. CJK) are the common case; a few scalars exercise coercion.
_LIST_ENTRY = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
)

# Each list section is one of: absent (key omitted), explicit null, an empty
# list, or a non-empty list. The first three must normalize to ``[]``.
_LIST_PRESENCE = st.sampled_from(["absent", "null", "empty", "nonempty"])


@st.composite
def _conforming_action_item(draw) -> dict:
    """A schema-conforming action item: a ``description`` plus an optional owner."""
    item: dict = {"description": draw(st.text(max_size=20))}
    if draw(st.booleans()):
        item["owner"] = draw(st.text(min_size=1, max_size=10))
    return item


@st.composite
def _conforming_minutes(draw):
    """Build a conforming minutes object plus a map of which sections are empty.

    Returns ``(raw, expected_empty)`` where ``raw`` is the JSON-serializable
    model output and ``expected_empty[key]`` says whether that list section
    should normalize to ``[]`` (it had no identified entries).
    """
    raw: dict = {}

    # summary: absent / null / a string -> always normalizes to a str.
    summary_kind = draw(st.sampled_from(["absent", "null", "str"]))
    if summary_kind == "null":
        raw["summary"] = None
    elif summary_kind == "str":
        raw["summary"] = draw(st.text(max_size=40))

    expected_empty: dict = {}
    for key in ("topics", "decisions"):
        kind = draw(_LIST_PRESENCE)
        if kind == "null":
            raw[key] = None
            expected_empty[key] = True
        elif kind == "empty":
            raw[key] = []
            expected_empty[key] = True
        elif kind == "nonempty":
            raw[key] = draw(st.lists(_LIST_ENTRY, min_size=1, max_size=5))
            expected_empty[key] = False
        else:  # absent
            expected_empty[key] = True

    kind = draw(_LIST_PRESENCE)
    if kind == "null":
        raw["action_items"] = None
        expected_empty["action_items"] = True
    elif kind == "empty":
        raw["action_items"] = []
        expected_empty["action_items"] = True
    elif kind == "nonempty":
        raw["action_items"] = draw(
            st.lists(_conforming_action_item(), min_size=1, max_size=5)
        )
        expected_empty["action_items"] = False
    else:  # absent
        expected_empty["action_items"] = True

    return raw, expected_empty


# Feature: meeting-minutes-service, Property 14: Conforming LLM output yields complete minutes with empty-list defaults
# Validates: Requirements 6.2
@settings(max_examples=100)
@given(case=_conforming_minutes())
def test_property_14_conforming_output_complete_with_empty_list_defaults(case):
    raw, expected_empty = case

    result = HttpLlmClient._parse_minutes(json.dumps(raw))

    # The minutes always carry exactly the four fields, with a str summary.
    assert set(result) == {"summary", "topics", "decisions", "action_items"}
    assert isinstance(result["summary"], str)

    # Every section is a list; sections with no identified entries are [].
    for key in ("topics", "decisions", "action_items"):
        assert isinstance(result[key], list)
        if expected_empty[key]:
            assert result[key] == []
        else:
            # A present non-empty list keeps exactly its entries (none dropped).
            assert len(result[key]) == len(raw[key])


# --------------------------------------------------------------------------- #
# Strategies for Property 15
# --------------------------------------------------------------------------- #
# A "real" owner: a value whose trimmed form is non-empty and is NOT the
# unassigned indicator (so "owner == UNASSIGNED exactly when none provided"
# stays unambiguous).
_REAL_OWNER = st.text(min_size=1, max_size=20).filter(
    lambda s: bool(s.strip()) and s.strip() != UNASSIGNED_OWNER
)
_WHITESPACE_OWNER = st.sampled_from([" ", "   ", "\t", "\n", " \t \n "])


@st.composite
def _action_item_case(draw):
    """One action item plus its expected normalized owner and provided-ness flag.

    Returns ``(item, expected_owner, provided_nonempty)``. ``provided_nonempty``
    is True only when a non-empty owner was supplied; in every other case
    (absent / null / empty / whitespace) the parser must default the owner to
    :data:`UNASSIGNED_OWNER`.
    """
    item: dict = {"description": draw(st.text(max_size=20))}
    kind = draw(st.sampled_from(["name", "absent", "null", "empty", "whitespace"]))
    if kind == "name":
        value = draw(_REAL_OWNER)
        item["owner"] = value
        return item, value.strip(), True
    if kind == "null":
        item["owner"] = None
    elif kind == "empty":
        item["owner"] = ""
    elif kind == "whitespace":
        item["owner"] = draw(_WHITESPACE_OWNER)
    # "absent" -> leave the owner key out entirely.
    return item, UNASSIGNED_OWNER, False


# Feature: meeting-minutes-service, Property 15: Every action item has a non-empty owner
# Validates: Requirements 6.3
@settings(max_examples=100)
@given(cases=st.lists(_action_item_case(), min_size=1, max_size=6))
def test_property_15_every_action_item_has_non_empty_owner(cases):
    items = [item for item, _owner, _provided in cases]
    raw = {"summary": "会议摘要", "action_items": items}

    result = HttpLlmClient._parse_minutes(json.dumps(raw))
    produced = result["action_items"]

    # No action item is dropped: one produced owner per input item.
    assert len(produced) == len(cases)
    for out, (_item, expected_owner, provided_nonempty) in zip(produced, cases):
        owner = out["owner"]
        # Every action item carries a non-empty owner string.
        assert isinstance(owner, str)
        assert owner != ""
        assert owner == expected_owner
        # Equal to the unassigned indicator EXACTLY when no owner was provided.
        assert (owner == UNASSIGNED_OWNER) == (not provided_nonempty)


# --------------------------------------------------------------------------- #
# Strategies for Property 16
# --------------------------------------------------------------------------- #
# Fresh-exception factories for the per-request timeout family + unreachable.
_TIMEOUT_FACTORIES = [
    lambda: httpx.ConnectTimeout("connect timed out"),
    lambda: httpx.ReadTimeout("read timed out"),
    lambda: httpx.WriteTimeout("write timed out"),
    lambda: httpx.PoolTimeout("pool timed out"),
]

# Values that are guaranteed-invalid for the field they corrupt.
_INVALID_SUMMARY = st.sampled_from([123, 1.5, True, ["x"], {"k": "v"}])
_INVALID_LIST = st.sampled_from(["不是数组", 7, True, {"k": "v"}])
_NON_SCALAR_ENTRY = st.sampled_from([None, ["a"], {"k": "v"}])

# Top-level content that decodes to a non-object, or does not decode at all.
_NON_OBJECT_CONTENT = st.sampled_from(
    [
        "not valid json {",
        "[1, 2, 3]",
        "42",
        "true",
        "null",
        '"just a string"',
    ]
)


@st.composite
def _nonconforming_object(draw) -> dict:
    """A JSON object guaranteed to violate the minutes schema in exactly one way."""
    obj: dict = {"summary": "摘要", "topics": [], "decisions": [], "action_items": []}
    which = draw(
        st.sampled_from(
            [
                "summary",
                "topics",
                "decisions",
                "action_items_not_list",
                "topic_entry",
                "action_not_object",
                "action_missing_desc",
                "action_bad_desc",
            ]
        )
    )
    if which == "summary":
        obj["summary"] = draw(_INVALID_SUMMARY)
    elif which == "topics":
        obj["topics"] = draw(_INVALID_LIST)
    elif which == "decisions":
        obj["decisions"] = draw(_INVALID_LIST)
    elif which == "action_items_not_list":
        obj["action_items"] = draw(st.sampled_from(["x", 5, {"k": "v"}]))
    elif which == "topic_entry":
        obj["topics"] = [draw(_NON_SCALAR_ENTRY)]
    elif which == "action_not_object":
        obj["action_items"] = [draw(st.sampled_from(["str", 5, ["a"]]))]
    elif which == "action_missing_desc":
        obj["action_items"] = [{"owner": "甲"}]
    else:  # action_bad_desc
        obj["action_items"] = [{"description": draw(_NON_SCALAR_ENTRY)}]
    return obj


# Non-conforming assistant *content* strings (well-formed envelope, bad content).
_BAD_CONTENT = st.one_of(
    _nonconforming_object().map(json.dumps),
    _NON_OBJECT_CONTENT,
)


@st.composite
def _failure_scenario(draw):
    """Build a ``(handler, expect_schema)`` failure scenario for ``generate_minutes``.

    ``expect_schema`` is True when the failure must surface as ``LlmSchemaError``
    (non-conforming content, Req 6.6) and False when it must surface as a plain
    ``LlmError`` that is *not* a schema error (transport-level failure, Req 6.5).
    """
    family = draw(st.sampled_from(["transport", "content"]))

    if family == "transport":
        kind = draw(st.sampled_from(["connect", "timeout", "status", "non_json"]))
        if kind == "connect":

            def handler(request):  # noqa: ANN001
                raise httpx.ConnectError("connection refused")

        elif kind == "timeout":
            factory = draw(st.sampled_from(_TIMEOUT_FACTORIES))

            def handler(request):  # noqa: ANN001
                raise factory()

        elif kind == "status":
            status = draw(st.sampled_from([400, 404, 429, 500, 502, 503]))

            def handler(request):  # noqa: ANN001
                return httpx.Response(status, json={"error": "boom"})

        else:  # non_json envelope body (transport-level malformation -> LlmError)

            def handler(request):  # noqa: ANN001
                return httpx.Response(
                    200,
                    content=b"<html>not json</html>",
                    headers={"content-type": "text/html"},
                )

        return handler, False

    # content family -> LlmSchemaError
    kind = draw(st.sampled_from(["bad_content", "non_string_content", "missing_content"]))
    if kind == "bad_content":
        content = draw(_BAD_CONTENT)

        def handler(request):  # noqa: ANN001
            return httpx.Response(200, json=_envelope(content))

    elif kind == "non_string_content":
        bad = draw(st.sampled_from([123, {"x": 1}, ["a"], True]))

        def handler(request):  # noqa: ANN001
            return httpx.Response(
                200, json={"choices": [{"message": {"content": bad}}]}
            )

    else:  # missing content key entirely

        def handler(request):  # noqa: ANN001
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant"}}]}
            )

    return handler, True


# Feature: meeting-minutes-service, Property 16: Terminal LLM failure fails the job with no partial minutes
# Validates: Requirements 6.5, 6.6
@settings(max_examples=100)
@given(scenario=_failure_scenario())
def test_property_16_terminal_failure_no_partial_minutes(scenario):
    handler, expect_schema = scenario
    client = _client()

    result = None
    with _mock_transport(handler):
        if expect_schema:
            # Non-conforming content -> LlmSchemaError (Req 6.6).
            with pytest.raises(LlmSchemaError):
                result = client.generate_minutes(_TRANSCRIPT)
        else:
            # Transport failure -> LlmError, never reported as a schema error (Req 6.5).
            with pytest.raises(LlmError) as exc_info:
                result = client.generate_minutes(_TRANSCRIPT)
            assert not isinstance(exc_info.value, LlmSchemaError)

    # No partial minutes ever leak: generate_minutes raised before returning.
    assert result is None


# --------------------------------------------------------------------------- #
# Unit test: request-timeout abort (Req 6.1 / 6.5)
# --------------------------------------------------------------------------- #
def test_request_timeout_aborts_with_no_partial_minutes(monkeypatch):
    """A per-request read timeout aborts the call -> LlmError, no partial minutes.

    Complements the transport-error/timeout-value example tests (task 6.1/6.3) by
    asserting the abort-on-timeout contract: the call does not hang or return a
    half-built result, it raises and yields nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    _install_mock_transport(monkeypatch, handler)

    client = HttpLlmClient(_settings())
    result = None
    with pytest.raises(LlmError) as exc_info:
        result = client.generate_minutes(_TRANSCRIPT)
    assert not isinstance(exc_info.value, LlmSchemaError)
    assert result is None


# --------------------------------------------------------------------------- #
# Unit test: Chinese-in -> Chinese-out via the offline MockLlmClient (Req 6.4)
# --------------------------------------------------------------------------- #
def test_mock_chinese_transcript_yields_chinese_minutes():
    """A Chinese transcript yields Chinese minutes via the deterministic mock.

    Validates Req 6.4 on the offline path (no real LLM_Service): every produced
    text field (summary, topics, decisions, action-item descriptions) contains
    Chinese characters, and every action item carries a non-empty owner.
    """
    client = MockLlmClient()
    transcript: TranscriptInput = {
        "segments": [
            {
                "start": 0.0,
                "end": 3.0,
                "text": "大家好，今天我们讨论第三季度的销售计划。",
                "speaker": "发言人A",
            },
            {
                "start": 3.0,
                "end": 6.0,
                "text": "我建议增加华东区的市场投入，并尽快完成预算审批。",
                "speaker": "发言人B",
            },
        ],
        "language": "zh",
    }

    minutes = client.generate_minutes(transcript)

    assert _has_chinese(minutes["summary"])
    for topic in minutes["topics"]:
        assert _has_chinese(topic)
    for decision in minutes["decisions"]:
        assert _has_chinese(decision)
    for item in minutes["action_items"]:
        assert _has_chinese(item["description"])
        assert isinstance(item["owner"], str) and item["owner"] != ""
