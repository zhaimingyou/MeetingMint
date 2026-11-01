"""Example/edge unit tests for HttpLlmClient minutes parsing/validation (task 6.2).

Task 6.2 hardens ``HttpLlmClient._parse_minutes`` from the first-cut defensive
parse into strict schema validation plus the full Req 6.2 / 6.3 normalization
semantics. These tests pin the *normalization* behaviour and *basic schema
acceptance/rejection*:

* a conforming response with every field -> correct ``MeetingMinutesData``;
* missing ``topics`` / ``decisions`` / ``action_items`` keys -> empty lists
  (Req 6.2);
* an action item lacking an owner, or with an empty/whitespace owner ->
  ``owner == UNASSIGNED_OWNER`` ("未指派") (Req 6.3);
* Chinese content round-trips unchanged (Req 6.4);
* schema-invalid shapes raise ``LlmSchemaError`` (Req 6.6, exercised lightly
  here; the full failure taxonomy is task 6.3 and the hypothesis properties
  14/15/16 are task 6.4).

The parser is a pure ``@staticmethod`` taking the model's JSON *content* string,
so these tests call it directly -- no httpx transport, no LLM_Service.
"""

from __future__ import annotations

import json

import pytest

from app.clients.base import LlmSchemaError
from app.clients.http import HttpLlmClient
from app.enums import UNASSIGNED_OWNER


def _parse(payload: dict | list | str) -> dict:
    """Serialize ``payload`` to a JSON string and run it through the parser."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return HttpLlmClient._parse_minutes(content)


# --------------------------------------------------------------------------- #
# Conforming response with all fields -> correct MeetingMinutesData
# --------------------------------------------------------------------------- #
def test_conforming_response_produces_full_minutes():
    result = _parse(
        {
            "summary": "本次会议讨论了项目排期与预算。",
            "topics": ["排期", "预算"],
            "decisions": ["按计划推进", "预算增加 10%"],
            "action_items": [
                {"description": "完成对接", "owner": "张伟"},
                {"description": "编写文档", "owner": "李娜"},
            ],
        }
    )

    assert result == {
        "summary": "本次会议讨论了项目排期与预算。",
        "topics": ["排期", "预算"],
        "decisions": ["按计划推进", "预算增加 10%"],
        "action_items": [
            {"description": "完成对接", "owner": "张伟"},
            {"description": "编写文档", "owner": "李娜"},
        ],
    }


# --------------------------------------------------------------------------- #
# Missing lists -> empty lists; missing summary -> "" (Req 6.2)
# --------------------------------------------------------------------------- #
def test_missing_lists_default_to_empty():
    result = _parse({"summary": "只有摘要"})

    assert result["summary"] == "只有摘要"
    assert result["topics"] == []
    assert result["decisions"] == []
    assert result["action_items"] == []


def test_missing_summary_defaults_to_empty_string():
    result = _parse({"topics": ["议题一"]})

    assert result["summary"] == ""
    assert result["topics"] == ["议题一"]


def test_explicit_null_lists_default_to_empty():
    result = _parse(
        {"summary": "s", "topics": None, "decisions": None, "action_items": None}
    )

    assert result["topics"] == []
    assert result["decisions"] == []
    assert result["action_items"] == []


# --------------------------------------------------------------------------- #
# Owner defaulting (Req 6.3)
# --------------------------------------------------------------------------- #
def test_action_item_missing_owner_defaults_to_unassigned():
    result = _parse(
        {
            "summary": "s",
            "action_items": [{"description": "无负责人的待办"}],
        }
    )

    assert result["action_items"] == [
        {"description": "无负责人的待办", "owner": UNASSIGNED_OWNER}
    ]
    assert UNASSIGNED_OWNER == "未指派"


@pytest.mark.parametrize("owner", ["", "   ", None])
def test_action_item_empty_or_null_owner_defaults_to_unassigned(owner):
    result = _parse(
        {
            "summary": "s",
            "action_items": [{"description": "待办", "owner": owner}],
        }
    )

    assert result["action_items"][0]["owner"] == UNASSIGNED_OWNER


def test_action_item_owner_is_trimmed():
    result = _parse(
        {
            "summary": "s",
            "action_items": [{"description": "待办", "owner": "  王芳  "}],
        }
    )

    assert result["action_items"][0]["owner"] == "王芳"


# --------------------------------------------------------------------------- #
# Type coercion of list entries / descriptions
# --------------------------------------------------------------------------- #
def test_scalar_list_entries_are_coerced_to_str():
    result = _parse(
        {"summary": "s", "topics": [1, 2.5, "三"], "decisions": [True]}
    )

    assert result["topics"] == ["1", "2.5", "三"]
    assert result["decisions"] == ["True"]


def test_scalar_description_is_coerced_to_str():
    result = _parse(
        {"summary": "s", "action_items": [{"description": 42, "owner": "甲"}]}
    )

    assert result["action_items"][0]["description"] == "42"


# --------------------------------------------------------------------------- #
# Chinese content round-trips unchanged (Req 6.4)
# --------------------------------------------------------------------------- #
def test_chinese_content_round_trips_unchanged():
    summary = "会议在下午三点开始，主要回顾上季度的销售业绩并制定下一步计划。"
    topics = ["季度业绩回顾", "市场推广策略", "人员招聘计划"]
    decisions = ["扩大华东区团队", "增加线上广告投放预算"]
    action_items = [
        {"description": "起草招聘方案", "owner": "陈静"},
        {"description": "对接广告供应商", "owner": "刘洋"},
    ]
    result = _parse(
        {
            "summary": summary,
            "topics": topics,
            "decisions": decisions,
            "action_items": action_items,
        }
    )

    # Every Chinese string is preserved byte-for-byte; nothing is translated,
    # transliterated, or stripped.
    assert result["summary"] == summary
    assert result["topics"] == topics
    assert result["decisions"] == decisions
    assert result["action_items"] == action_items


# --------------------------------------------------------------------------- #
# Schema-invalid cases raise LlmSchemaError (Req 6.6; light here)
# --------------------------------------------------------------------------- #
def test_non_json_content_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        HttpLlmClient._parse_minutes("not json at all {")


def test_top_level_not_object_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse(["just", "a", "list"])


def test_summary_not_a_string_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": 123, "topics": []})


def test_topics_not_a_list_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "topics": "排期"})


def test_decisions_not_a_list_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "decisions": {"k": "v"}})


def test_non_scalar_topic_entry_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "topics": [{"nested": "object"}]})


def test_action_items_not_a_list_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "action_items": {"description": "x"}})


def test_action_item_not_an_object_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "action_items": ["just a string"]})


def test_action_item_missing_description_raises_schema_error():
    with pytest.raises(LlmSchemaError):
        _parse({"summary": "s", "action_items": [{"owner": "甲"}]})
