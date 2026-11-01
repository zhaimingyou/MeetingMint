"""Deterministic, offline mock ASR/LLM clients (task 4.2).

These implementations let the entire pipeline run and be tested with no real
ASR_Service or LLM_Service present (Req 11.3, 11.4, 11.5). They satisfy the
:class:`~app.clients.base.AsrClient` / :class:`~app.clients.base.LlmClient`
Protocols, perform **no** HTTP/network I/O, and require **no** endpoint URL or
model name. Identical input yields byte-for-byte identical output, both across
repeated calls in one process and across separate process runs.

Determinism rule
----------------
All variability is derived from a *stable* digest computed with
:func:`hashlib.sha256` -- never Python's built-in :func:`hash`, whose string
hashing is randomized per process (``PYTHONHASHSEED``). Concretely:

* :class:`MockAsrClient.transcribe` seeds from the audio's content when the path
  exists (the file bytes), and otherwise from the path string itself, encoded as
  UTF-8. Reading bytes when available means two different paths holding identical
  audio transcribe identically, while a missing/synthetic path still produces a
  deterministic result from its name. The choice is documented and stable.
* :class:`MockLlmClient.generate_minutes` seeds from the transcript content: the
  ordered ``(start, end, text, speaker)`` tuples plus the optional ``language``
  hint, serialized deterministically. The language hint (or detected Chinese
  characters in the text) selects Chinese output so Req 6.4 holds for the mock.

From a seed digest we derive small integers (segment counts, list lengths,
owner choices, timings) by slicing bytes of the digest, so every derived value
is a pure function of the input.
"""

from __future__ import annotations

import hashlib
import os

from app.clients.types import (
    ActionItemData,
    MeetingMinutesData,
    TranscriptInput,
    TranscriptSegment,
)
from app.enums import UNASSIGNED_OWNER

__all__ = ["MockAsrClient", "MockLlmClient"]


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #
def _int_from_digest(digest: bytes, offset: int, size: int = 4) -> int:
    """Read ``size`` bytes from ``digest`` (wrapping) as an unsigned int.

    Pure function of ``(digest, offset, size)`` so callers get stable, repeatable
    pseudo-random integers without any global RNG state.
    """
    out = 0
    n = len(digest)
    for i in range(size):
        out = (out << 8) | digest[(offset + i) % n]
    return out


def _bounded(digest: bytes, offset: int, low: int, high: int) -> int:
    """Map digest bytes into the inclusive range ``[low, high]`` deterministically."""
    if high <= low:
        return low
    span = high - low + 1
    return low + (_int_from_digest(digest, offset) % span)


# Fixed Chinese placeholder vocabularies. Indexing into these by digest-derived
# integers keeps output well-formed and deterministic while resembling real
# Chinese meeting content (so Req 6.4 holds for the mock).
_ASR_PHRASES: tuple[str, ...] = (
    "大家好，我们现在开始今天的会议。",
    "首先回顾一下上周的工作进展。",
    "关于项目排期，我有几点想说明。",
    "这个方案需要进一步讨论确认。",
    "请相关同事在本周内完成对接。",
    "下面我们看一下预算的分配情况。",
    "测试阶段目前进展顺利。",
    "有没有其他需要补充的内容？",
    "好的，那这一项就先这样定下来。",
    "感谢大家参与，会议到此结束。",
)

_SPEAKERS: tuple[str, ...] = ("发言人A", "发言人B", "发言人C")

_OWNERS: tuple[str, ...] = ("张伟", "李娜", "王芳", "刘强", "陈静")

_TOPIC_PHRASES: tuple[str, ...] = (
    "项目进度回顾",
    "预算分配",
    "测试计划",
    "排期安排",
    "风险与问题",
    "下一步计划",
)

_DECISION_PHRASES: tuple[str, ...] = (
    "确定按现有方案推进项目。",
    "预算维持原计划不变。",
    "测试在本周内完成。",
    "排期顺延一周。",
    "由专人负责对接事宜。",
)

_ACTION_PHRASES: tuple[str, ...] = (
    "完成接口对接",
    "整理会议纪要并分发",
    "提交测试报告",
    "更新项目排期表",
    "跟进预算审批",
)


# --------------------------------------------------------------------------- #
# MockAsrClient
# --------------------------------------------------------------------------- #
class MockAsrClient:
    """Offline, deterministic ASR_Client (Req 11.3).

    Satisfies :class:`app.clients.base.AsrClient`. Takes no constructor
    arguments and ignores any endpoint configuration: it can run when
    ``asr_endpoint_url`` is absent (Req 11.5). It never opens a socket.

    The transcript is a pure function of the input audio: identical bytes (or an
    identical path string when the file does not exist) always yield the same
    ordered, well-formed segments.
    """

    def _seed(self, audio_path: str) -> bytes:
        """Compute the stable seed digest for ``audio_path`` (see module docstring)."""
        hasher = hashlib.sha256()
        try:
            if audio_path and os.path.isfile(audio_path):
                with open(audio_path, "rb") as fh:
                    for block in iter(lambda: fh.read(65536), b""):
                        hasher.update(block)
            else:
                hasher.update(audio_path.encode("utf-8"))
        except OSError:
            # Any read problem falls back to hashing the path string so the
            # result stays deterministic and the client never raises here.
            hasher = hashlib.sha256(audio_path.encode("utf-8"))
        return hasher.digest()

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Return deterministic, ascending-ordered, contiguous segments (>= 1)."""
        digest = self._seed(audio_path)

        # 3..8 segments, always at least one.
        count = _bounded(digest, 0, 3, 8)

        segments: list[TranscriptSegment] = []
        cursor = 0.0
        for i in range(count):
            # Each segment is 1.0..6.0s long; contiguous so end[i] == start[i+1].
            duration = _bounded(digest, 4 + i, 10, 60) / 10.0
            start = round(cursor, 3)
            end = round(cursor + duration, 3)
            text = _ASR_PHRASES[_int_from_digest(digest, 16 + i) % len(_ASR_PHRASES)]
            # Attach a speaker label to roughly every other segment to exercise
            # diarization downstream; deterministic from the digest.
            if _int_from_digest(digest, 24 + i) % 2 == 0:
                speaker: str | None = _SPEAKERS[
                    _int_from_digest(digest, 28 + i) % len(_SPEAKERS)
                ]
            else:
                speaker = None
            segments.append(
                {"start": start, "end": end, "text": text, "speaker": speaker}
            )
            cursor = end

        return segments


# --------------------------------------------------------------------------- #
# MockLlmClient
# --------------------------------------------------------------------------- #
class MockLlmClient:
    """Offline, deterministic LLM_Client (Req 11.4).

    Satisfies :class:`app.clients.base.LlmClient`. Takes no constructor
    arguments and ignores any endpoint/model configuration: it can run when
    ``llm_endpoint_url``/``llm_model_name`` are absent (Req 11.5). It never opens
    a socket.

    The minutes are a pure function of the transcript content and are always
    schema-valid: a string ``summary`` plus ``topics``/``decisions`` string
    lists and ``action_items`` each carrying a non-empty ``owner`` (some set to
    :data:`app.enums.UNASSIGNED_OWNER`, others to a deterministic name). Empty
    transcripts still yield a valid object.
    """

    def _seed(self, transcript: TranscriptInput) -> bytes:
        """Stable digest over ordered segment tuples + language hint."""
        hasher = hashlib.sha256()
        language = transcript.get("language")
        hasher.update(f"lang={language!r}\n".encode("utf-8"))
        for seg in transcript.get("segments", []):
            line = (
                f"{seg.get('start')!r}|{seg.get('end')!r}|"
                f"{seg.get('text', '')}|{seg.get('speaker')!r}\n"
            )
            hasher.update(line.encode("utf-8"))
        return hasher.digest()

    def generate_minutes(self, transcript: TranscriptInput) -> MeetingMinutesData:
        """Return deterministic, schema-valid Chinese minutes."""
        digest = self._seed(transcript)

        # topics: 1..4, decisions: 0..3, action_items: 0..4 (empty-list-friendly).
        n_topics = _bounded(digest, 0, 1, 4)
        n_decisions = _bounded(digest, 4, 0, 3)
        n_actions = _bounded(digest, 8, 0, 4)

        topics = [
            _TOPIC_PHRASES[_int_from_digest(digest, 12 + i) % len(_TOPIC_PHRASES)]
            for i in range(n_topics)
        ]
        decisions = [
            _DECISION_PHRASES[
                _int_from_digest(digest, 20 + i) % len(_DECISION_PHRASES)
            ]
            for i in range(n_decisions)
        ]

        action_items: list[ActionItemData] = []
        for i in range(n_actions):
            description = _ACTION_PHRASES[
                _int_from_digest(digest, 28 + i) % len(_ACTION_PHRASES)
            ]
            # Roughly one third of items are unassigned; the rest get a stable
            # owner name. Owner is always non-empty (Req 6.3).
            if _int_from_digest(digest, 36 + i) % 3 == 0:
                owner = UNASSIGNED_OWNER
            else:
                owner = _OWNERS[_int_from_digest(digest, 40 + i) % len(_OWNERS)]
            action_items.append({"description": description, "owner": owner})

        segment_count = len(transcript.get("segments", []))
        summary = (
            f"本次会议共讨论了{n_topics}个议题，"
            f"形成{n_decisions}项决议，"
            f"明确{n_actions}项待办事项。"
            f"会议记录基于{segment_count}个转写片段生成。"
        )

        return {
            "summary": summary,
            "topics": topics,
            "decisions": decisions,
            "action_items": action_items,
        }

    def correct_text(self, text: str, *, context: str | None = None) -> str:
        """Mock correction: return the text unchanged (no-op in mock mode)."""
        return text

    def correct_batch(
        self, segments: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Mock batch correction: return each segment text unchanged."""
        return [
            {"index": int(seg.get("index", i)), "text": str(seg.get("text", ""))}
            for i, seg in enumerate(segments)
        ]
