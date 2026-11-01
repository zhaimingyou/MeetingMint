"""ASR_Client / LLM_Client interfaces and client exceptions (task 4.1).

This module defines the *pluggable boundary* between the application and the
external speech-recognition and minutes-generation services (design: "ASR_Client
/ LLM_Client" components). It declares:

* :class:`AsrClient` and :class:`LlmClient` -- structural (``typing.Protocol``)
  interfaces that both the HTTP implementations (tasks 5, 6) and the mock
  implementations (task 4.2) satisfy, and that the factories (task 4.3) return.
* The client exception hierarchy -- :class:`AsrError`, :class:`LlmError`, and
  :class:`LlmSchemaError` -- used by the worker pipeline (task 9) to map a
  client failure to ``Job_Status = failed`` with an error description.

The module is intentionally dependency-light: no ``httpx``, no database, no
settings. It imports only the typed shapes from :mod:`app.clients.types`. This
keeps the interface importable from anywhere (tests, worker, factories) without
pulling in transport or persistence machinery.

The Protocols are marked ``@runtime_checkable`` so tests and the factories can
use ``isinstance(obj, AsrClient)`` to assert an implementation conforms. Note
that ``runtime_checkable`` only verifies method *presence*, not signatures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.clients.types import (
    MeetingMinutesData,
    TranscriptInput,
    TranscriptSegment,
)

__all__ = [
    "AsrClient",
    "LlmClient",
    "AsrError",
    "LlmError",
    "LlmSchemaError",
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class AsrError(Exception):
    """Terminal failure of the ASR_Client (Req 5.5, 5.6).

    Raised when the ASR_Service is unreachable, times out, or returns an error
    after the configured retries are exhausted -- including the case where a
    specific chunk fails. The worker translates this into ``Job_Status =
    failed`` and persists no partial transcript.

    :param message: human-readable error description recorded on the job.
    :param chunk_index: 0-based index of the chunk that failed, when the failure
        is attributable to a specific chunk (Req 5.6); ``None`` for whole-request
        failures. Exposed as the ``chunk_index`` attribute.
    """

    def __init__(self, message: str = "", *, chunk_index: int | None = None) -> None:
        super().__init__(message)
        self.chunk_index = chunk_index

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        base = super().__str__()
        if self.chunk_index is not None:
            suffix = f"chunk {self.chunk_index}"
            return f"{base} ({suffix})" if base else suffix
        return base


class LlmError(Exception):
    """Terminal failure of the LLM_Client transport (Req 6.5).

    Raised when the LLM_Service is unreachable, times out, or returns a
    transport/HTTP error. The worker translates this into ``Job_Status =
    failed`` and persists no partial minutes.

    :param message: human-readable error description recorded on the job.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class LlmSchemaError(LlmError):
    """The LLM returned content that does not conform to the schema (Req 6.6).

    Subclasses :class:`LlmError` because a schema-invalid response is a kind of
    LLM failure: code that only needs "the LLM step failed" can catch
    ``LlmError`` and handle both, while code that wants to distinguish a
    well-formed transport success carrying malformed content (e.g. for a more
    specific error description) can catch ``LlmSchemaError`` explicitly. The
    worker records a parsing-error description and persists no partial minutes.
    """


# --------------------------------------------------------------------------- #
# Interfaces (Protocols)
# --------------------------------------------------------------------------- #
@runtime_checkable
class AsrClient(Protocol):
    """Speech-to-text boundary (design: ASR_Client).

    Implementations: ``HttpAsrClient`` (task 5) and ``MockAsrClient`` (task 4.2),
    selected at startup by ``make_asr_client(settings)`` (task 4.3).
    """

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Transcribe Standardized_Audio at ``audio_path`` into segments.

        :param audio_path: filesystem path to the mono 16 kHz WAV produced by
            the Audio_Preprocessor.
        :returns: the assembled transcript segments ordered by ascending
            ``start`` timestamp (Req 5.3). Speaker labels are included only when
            the ASR_Service supplies diarization data (Req 5.4).
        :raises AsrError: if the service is unreachable, times out, or returns
            an error after the configured retries are exhausted (Req 5.5); when
            a specific chunk is at fault the error carries ``chunk_index``
            (Req 5.6). No partial transcript is persisted on failure.
        """
        ...


@runtime_checkable
class LlmClient(Protocol):
    """Minutes-generation boundary (design: LLM_Client).

    Implementations: ``HttpLlmClient`` (task 6) and ``MockLlmClient`` (task 4.2),
    selected at startup by ``make_llm_client(settings)`` (task 4.3).
    """

    def generate_minutes(self, transcript: TranscriptInput) -> MeetingMinutesData:
        """Generate structured minutes from an assembled transcript.

        :param transcript: the :class:`~app.clients.types.TranscriptInput`
            assembled by the worker (ordered segments + optional language hint).
        :returns: validated :class:`~app.clients.types.MeetingMinutesData` with a
            summary and three lists; absent lists are empty (Req 6.2) and every
            action item carries a non-empty ``owner`` (Req 6.3). A Chinese
            transcript yields Chinese minutes (Req 6.4).
        :raises LlmError: on transport failure or timeout (Req 6.5); no partial
            minutes are persisted.
        :raises LlmSchemaError: when the returned content does not conform to the
            expected schema (Req 6.6); no partial minutes are persisted.
        """
        ...

    def correct_text(self, text: str, *, context: str | None = None) -> str:
        """Post-ASR phonetic correction for a selected text span.

        :param text: raw ASR transcript text to correct.
        :param context: optional surrounding context to aid disambiguation.
        :returns: corrected text as a plain string.
        :raises LlmError: on transport failure or timeout.
        """
        ...

    def correct_batch(
        self, segments: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Post-ASR phonetic correction for a batch of segments.

        Each input segment is a dict with keys ``index`` (int), ``text`` (str),
        and optionally ``speaker`` (str). The method sends segments to the LLM
        in windowed prompts (group ~25 segments per window, overlap 2 for
        context continuity), and returns a list of ``{index, text}`` dicts
        aligned by the original indices.

        :param segments: ordered list of segment dicts to correct.
        :returns: list of ``{index: int, text: str}`` dicts, one per input
            segment, preserving the original index ordering.
        :raises LlmError: on transport failure or timeout.
        """
        ...
