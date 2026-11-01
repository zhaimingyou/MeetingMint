"""Unit tests for the ASR/LLM client contracts (task 4.1).

These are light example-based tests confirming that:
  * the typed shapes construct as plain dicts with the documented fields,
  * the ``AsrClient`` / ``LlmClient`` Protocols are runtime-checkable and a
    trivial in-test stub satisfies them, and
  * the exception hierarchy is correct and carries context (e.g. an ``AsrError``
    chunk index).

No real clients, HTTP, or database are involved.
"""

from __future__ import annotations

from app.clients import (
    ActionItemData,
    AsrClient,
    AsrError,
    LlmClient,
    LlmError,
    LlmSchemaError,
    MeetingMinutesData,
    TranscriptInput,
    TranscriptSegment,
)
from app.enums import UNASSIGNED_OWNER


# --------------------------------------------------------------------------- #
# In-test stubs (structurally satisfy the Protocols, no HTTP)
# --------------------------------------------------------------------------- #
class _StubAsr:
    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        return [
            {"start": 0.0, "end": 1.5, "text": "你好", "speaker": None},
            {"start": 1.5, "end": 3.0, "text": "世界", "speaker": "A"},
        ]


class _StubLlm:
    def generate_minutes(self, transcript: TranscriptInput) -> MeetingMinutesData:
        return {
            "summary": "摘要",
            "topics": [],
            "decisions": [],
            "action_items": [{"description": "跟进", "owner": UNASSIGNED_OWNER}],
        }

    def correct_text(self, text: str, *, context: str | None = None) -> str:
        return text

    def correct_batch(
        self, segments: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        return [
            {"index": int(s.get("index", i)), "text": str(s.get("text", ""))}
            for i, s in enumerate(segments)
        ]


# --------------------------------------------------------------------------- #
# Typed shapes
# --------------------------------------------------------------------------- #
def test_transcript_segment_constructs_with_fields():
    seg: TranscriptSegment = {"start": 0.0, "end": 2.0, "text": "hi", "speaker": None}
    assert seg["start"] == 0.0
    assert seg["end"] == 2.0
    assert seg["text"] == "hi"
    assert seg["speaker"] is None


def test_transcript_segment_carries_speaker_when_present():
    seg: TranscriptSegment = {"start": 1.0, "end": 2.0, "text": "x", "speaker": "B"}
    assert seg["speaker"] == "B"


def test_action_item_data_constructs():
    item: ActionItemData = {"description": "do thing", "owner": "Alice"}
    assert item["description"] == "do thing"
    assert item["owner"] == "Alice"


def test_meeting_minutes_data_constructs_with_empty_lists():
    minutes: MeetingMinutesData = {
        "summary": "s",
        "topics": [],
        "decisions": [],
        "action_items": [],
    }
    assert minutes["summary"] == "s"
    assert minutes["topics"] == []
    assert minutes["decisions"] == []
    assert minutes["action_items"] == []


def test_transcript_input_with_and_without_language():
    seg: TranscriptSegment = {"start": 0.0, "end": 1.0, "text": "t", "speaker": None}
    with_lang: TranscriptInput = {"segments": [seg], "language": "zh"}
    without_lang: TranscriptInput = {"segments": [seg]}
    assert with_lang["segments"] == [seg]
    assert with_lang["language"] == "zh"
    assert "language" not in without_lang


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #
def test_stub_satisfies_asr_protocol():
    stub = _StubAsr()
    assert isinstance(stub, AsrClient)
    segments = stub.transcribe("/tmp/audio.wav")
    assert [s["start"] for s in segments] == sorted(s["start"] for s in segments)


def test_stub_satisfies_llm_protocol():
    stub = _StubLlm()
    assert isinstance(stub, LlmClient)
    minutes = stub.generate_minutes({"segments": []})
    assert set(minutes) == {"summary", "topics", "decisions", "action_items"}
    assert all(item["owner"] for item in minutes["action_items"])


def test_object_missing_method_does_not_satisfy_protocol():
    class NotAClient:
        pass

    assert not isinstance(NotAClient(), AsrClient)
    assert not isinstance(NotAClient(), LlmClient)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
def test_asr_error_default_chunk_index_is_none():
    err = AsrError("service unreachable")
    assert err.chunk_index is None
    assert "service unreachable" in str(err)


def test_asr_error_carries_chunk_index():
    err = AsrError("chunk failed after retries", chunk_index=3)
    assert err.chunk_index == 3
    assert "chunk 3" in str(err)


def test_llm_error_is_exception():
    err = LlmError("timeout")
    assert isinstance(err, Exception)
    assert "timeout" in str(err)


def test_llm_schema_error_is_a_kind_of_llm_error():
    err = LlmSchemaError("missing summary field")
    assert isinstance(err, LlmSchemaError)
    assert isinstance(err, LlmError)
    assert isinstance(err, Exception)


def test_exception_hierarchy_distinct():
    # AsrError and LlmError are unrelated branches.
    assert not issubclass(AsrError, LlmError)
    assert not issubclass(LlmError, AsrError)
