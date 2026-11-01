"""Typed shapes for incremental streaming-ASR results (task 3.1).

Plain, dependency-light :class:`typing.TypedDict` shapes the
Streaming_ASR_Client produces, matching the style of :mod:`app.clients.types`
(zero runtime weight, built straight from parsed JSON). Field invariants are
documented here and guaranteed by the producing implementations and their
property tests, not runtime-checked by these structural types.
"""

from __future__ import annotations

from typing import TypedDict


class StreamingSegment(TypedDict):
    """One incremental transcript segment (design: Streaming_ASR_Client).

    Invariants (guaranteed by producers, not enforced by this type):
      * ``start`` is in seconds from session start and ``>= 0``.
      * ``end`` is in seconds and ``>= start``.
      * ``text`` is the recognized text for ``[start, end]``.
      * ``speaker`` is populated only when the Streaming_ASR_Service supplies
        diarization data (Req 3.6); otherwise it is ``None``.
      * ``is_final`` is ``True`` for a Finalized_Segment (stable; not revised)
        and ``False`` for a Partial_Segment (provisional; may be revised by a
        later result) (Req 3.4).
    """

    start: float
    end: float
    text: str
    speaker: str | None
    is_final: bool


class StreamingAsrResult(TypedDict):
    """One incremental update returned by :meth:`StreamingAsrSession.push`.

    ``segments`` holds zero or more :class:`StreamingSegment`: the
    Finalized_Segments (ordered by ascending ``start``) followed by an optional
    trailing Partial_Segment (Req 3.4, 3.5).
    """

    segments: list[StreamingSegment]
