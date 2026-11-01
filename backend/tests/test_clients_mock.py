"""Unit tests for the deterministic mock ASR/LLM clients (task 4.2).

Example-based tests confirming the mocks:
  * structurally satisfy the ``AsrClient`` / ``LlmClient`` Protocols,
  * are deterministic (identical input => identical output across repeated
    calls),
  * produce well-formed ASR output (>= 1 segment, ascending start, end >= start,
    contiguous timings),
  * produce schema-valid LLM minutes (summary str + three lists, every action
    item carrying a non-empty owner; empty-transcript-friendly), in Chinese
    (Req 6.4),
  * require no endpoint URL / model name and perform no network I/O.

The hypothesis property tests for determinism/well-formedness/offline behavior
are task 4.4; these are the example tests for task 4.2.
"""

from __future__ import annotations

import socket

import pytest

from app.clients import (
    AsrClient,
    LlmClient,
    MockAsrClient,
    MockLlmClient,
)
from app.clients.types import TranscriptInput
from app.enums import UNASSIGNED_OWNER


# --------------------------------------------------------------------------- #
# Network guard: any attempt to open a socket fails the test (proves offline).
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):  # pragma: no cover - only hit on violation
        raise AssertionError("mock client attempted network I/O")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


# --------------------------------------------------------------------------- #
# Protocol conformance (no constructor args required)
# --------------------------------------------------------------------------- #
def test_mock_asr_satisfies_protocol():
    assert isinstance(MockAsrClient(), AsrClient)


def test_mock_llm_satisfies_protocol():
    assert isinstance(MockLlmClient(), LlmClient)


def test_mocks_take_no_constructor_args():
    # Construct with no args and no settings/endpoint info (Req 11.5).
    MockAsrClient()
    MockLlmClient()


# --------------------------------------------------------------------------- #
# MockAsrClient: determinism + well-formedness + offline
# --------------------------------------------------------------------------- #
def test_asr_is_deterministic_for_missing_path(no_network):
    client = MockAsrClient()
    first = client.transcribe("/tmp/does-not-exist-123.wav")
    second = client.transcribe("/tmp/does-not-exist-123.wav")
    assert first == second


def test_asr_is_deterministic_for_real_file(tmp_path, no_network):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFsome-fake-audio-bytes-for-hashing")
    client = MockAsrClient()
    assert client.transcribe(str(audio)) == client.transcribe(str(audio))


def test_asr_hashes_file_contents_not_path(tmp_path):
    # Two different paths with identical bytes transcribe identically.
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(b"identical-bytes")
    b.write_bytes(b"identical-bytes")
    client = MockAsrClient()
    assert client.transcribe(str(a)) == client.transcribe(str(b))


def test_asr_different_input_generally_differs():
    client = MockAsrClient()
    assert client.transcribe("meeting-one.wav") != client.transcribe(
        "completely-different-name.wav"
    )


def test_asr_segments_well_formed(no_network):
    client = MockAsrClient()
    segments = client.transcribe("/tmp/sample.wav")

    assert len(segments) >= 1
    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)  # ascending by start

    prev_end = None
    for seg in segments:
        assert seg["start"] >= 0.0
        assert seg["end"] >= seg["start"]
        assert isinstance(seg["text"], str) and seg["text"]
        assert seg["speaker"] is None or isinstance(seg["speaker"], str)
        if prev_end is not None:
            assert seg["start"] == prev_end  # contiguous
        prev_end = seg["end"]


def test_asr_includes_some_speaker_labels_across_inputs():
    # Exercises diarization downstream: at least some input yields a speaker.
    client = MockAsrClient()
    seen_speaker = False
    for i in range(20):
        for seg in client.transcribe(f"input-{i}.wav"):
            if seg["speaker"] is not None:
                seen_speaker = True
    assert seen_speaker


# --------------------------------------------------------------------------- #
# MockLlmClient: determinism + schema validity + offline + Chinese
# --------------------------------------------------------------------------- #
def _transcript(text: str = "你好，我们开始开会。", language: str | None = "zh") -> TranscriptInput:
    seg = {"start": 0.0, "end": 2.0, "text": text, "speaker": "发言人A"}
    if language is None:
        return {"segments": [seg]}
    return {"segments": [seg], "language": language}


def test_llm_is_deterministic(no_network):
    client = MockLlmClient()
    t = _transcript()
    assert client.generate_minutes(t) == client.generate_minutes(t)


def test_llm_different_transcripts_generally_differ():
    client = MockLlmClient()
    a = _transcript("讨论预算问题")
    b = _transcript("安排测试计划与排期")
    assert client.generate_minutes(a) != client.generate_minutes(b)


def test_llm_output_is_schema_valid(no_network):
    client = MockLlmClient()
    minutes = client.generate_minutes(_transcript())

    assert set(minutes) == {"summary", "topics", "decisions", "action_items"}
    assert isinstance(minutes["summary"], str) and minutes["summary"]
    assert isinstance(minutes["topics"], list)
    assert all(isinstance(t, str) and t for t in minutes["topics"])
    assert isinstance(minutes["decisions"], list)
    assert all(isinstance(d, str) and d for d in minutes["decisions"])
    assert isinstance(minutes["action_items"], list)
    for item in minutes["action_items"]:
        assert set(item) == {"description", "owner"}
        assert isinstance(item["description"], str) and item["description"]
        assert isinstance(item["owner"], str) and item["owner"]  # non-empty owner


def test_llm_handles_empty_transcript(no_network):
    client = MockLlmClient()
    minutes = client.generate_minutes({"segments": []})
    assert set(minutes) == {"summary", "topics", "decisions", "action_items"}
    assert isinstance(minutes["summary"], str) and minutes["summary"]
    # Empty-list-friendly: lists are valid lists (may be empty for decisions/items).
    assert isinstance(minutes["topics"], list)
    assert isinstance(minutes["decisions"], list)
    assert isinstance(minutes["action_items"], list)


def test_llm_output_is_chinese():
    client = MockLlmClient()
    minutes = client.generate_minutes(_transcript())
    assert any("\u4e00" <= ch <= "\u9fff" for ch in minutes["summary"])


def test_llm_works_without_language_hint(no_network):
    client = MockLlmClient()
    minutes = client.generate_minutes(_transcript(language=None))
    assert any("\u4e00" <= ch <= "\u9fff" for ch in minutes["summary"])


def test_llm_uses_unassigned_owner_for_some_inputs():
    # Across a spread of inputs the unassigned indicator appears at least once.
    client = MockLlmClient()
    seen_unassigned = False
    for i in range(30):
        minutes = client.generate_minutes(_transcript(f"议题 {i} 的讨论内容"))
        if any(item["owner"] == UNASSIGNED_OWNER for item in minutes["action_items"]):
            seen_unassigned = True
    assert seen_unassigned
