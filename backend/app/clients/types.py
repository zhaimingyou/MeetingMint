"""Typed data shapes exchanged with the ASR and LLM clients (task 4.1).

These are the plain, dependency-light data shapes that the ASR_Client and
LLM_Client interfaces (see :mod:`app.clients.base`) produce and consume. They
deliberately use :class:`typing.TypedDict` rather than dataclasses or Pydantic
models for three reasons:

1. **Fidelity to the design.** The design document's "ASR_Client Interface" and
   "LLM_Client Interface" sections specify these exact shapes as ``TypedDict``s.
2. **Zero runtime weight.** A ``TypedDict`` is just a ``dict`` at runtime, so the
   mock clients (task 4.2) and the HTTP clients (tasks 5/6) can build results
   directly from parsed JSON without an extra construction/validation layer,
   and the worker (task 9) can hand the same dicts straight to the persistence
   layer. The DB models (`app.models.TranscriptSegment`, `ActionItem`, ...)
   remain the validation boundary at write time.
3. **Structural typing.** Callers may construct these as ordinary dict literals,
   which keeps the ASR/LLM HTTP-response parsing code simple.

Field-level invariants (``start >= 0``, ``end >= start``, owner non-empty, etc.)
are documented here and enforced by the producing implementations and their
property tests (tasks 5, 6) and by the database schema (NOT-NULL columns); they
are intentionally NOT runtime-checked by these structural types.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TranscriptSegment(TypedDict):
    """One time-bounded segment of a transcript (design: ASR_Client Interface).

    Invariants (guaranteed by producers, not enforced by this type):
      * ``start`` is in seconds and ``>= 0``.
      * ``end`` is in seconds and ``>= start``.
      * ``text`` is the recognized text for ``[start, end]``.
      * ``speaker`` is populated only when the ASR_Service supplies diarization
        data (Req 5.4); otherwise it is ``None``.

    The assembled list returned by :meth:`AsrClient.transcribe` is ordered by
    ascending ``start`` (Req 5.3). These fields map one-to-one onto
    ``app.models.TranscriptSegment`` columns (``start_seconds``/``end_seconds``/
    ``text``/``speaker``); ``ordinal`` is assigned by the persistence layer from
    list position, so it is not part of this in-flight shape.
    """

    start: float
    end: float
    text: str
    speaker: str | None


class ActionItemData(TypedDict):
    """A single action item within generated minutes (design: LLM_Client).

    ``owner`` is always present and non-empty: the LLM_Client substitutes the
    unassigned indicator (``app.enums.UNASSIGNED_OWNER`` == ``"未指派"``) when no
    owner is identified in the transcript (Req 6.3), so consumers never have to
    handle a missing/None owner.
    """

    description: str
    owner: str


class MeetingMinutesData(TypedDict):
    """The structured minutes returned by :meth:`LlmClient.generate_minutes`.

    Conforms to the schema in Req 6.2: a ``summary`` plus three lists. Any list
    for which no entries are identified is an empty list (never ``None``), so
    consumers can iterate unconditionally.
    """

    summary: str
    topics: list[str]
    decisions: list[str]
    action_items: list[ActionItemData]


class TranscriptInput(TypedDict):
    """The transcript payload handed to :meth:`LlmClient.generate_minutes`.

    The design's ``LLM_Client.generate_minutes(transcript: Transcript)`` takes a
    "Transcript". This is the concrete, lightweight in-memory shape the worker
    (task 9) assembles from the ASR result and passes to the LLM client -- it is
    NOT the ORM ``app.models.Transcript`` row. It carries the assembled
    ``segments`` (already ordered by ascending ``start``) plus an optional
    ``language`` hint so the LLM client can honor "Chinese transcript => Chinese
    minutes" (Req 6.4) without re-detecting language.

    Consumers (the HTTP and mock LLM clients) should treat ``language`` as
    advisory and tolerate its absence. Tasks 6 and 9 MUST construct and consume
    this same shape so the contract stays consistent.
    """

    segments: list[TranscriptSegment]
    language: NotRequired[str | None]
