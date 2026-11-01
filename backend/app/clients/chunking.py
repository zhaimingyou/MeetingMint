"""Audio chunking math + duration probing for the HTTP ASR client (task 5.1).

This module holds the *pure* chunking logic and the ffprobe-based duration
probe that :class:`~app.clients.http.HttpAsrClient` uses (in task 5.2) to split
Standardized_Audio into per-request windows before transcription.

Two concerns are deliberately separated so the chunking math can be tested
without a real audio file or a real ``ffprobe`` binary:

* :func:`compute_chunks` -- a **pure function** of ``(duration, threshold)``
  returning the sequential, non-overlapping ``(start, end)`` windows. This is
  the function exercised by Property 10 (task 5.4) and the example tests for
  task 5.1; it has no I/O.
* :func:`probe_duration` -- shells out to ``ffprobe`` to read an audio file's
  duration in seconds. Its output parsing is factored into
  :func:`parse_ffprobe_duration` so the parse can be unit-tested against a
  captured ``ffprobe`` payload without invoking the binary.

Injection seam for task 5.2 / tests
------------------------------------
:class:`~app.clients.http.HttpAsrClient` accepts an optional
``duration_prober`` callable (defaulting to :func:`probe_duration`). Tests and
the worker can pass a fake prober (e.g. ``lambda path: 12.5``) so chunk planning
is exercised end to end without ffprobe or a real file. The client's
:meth:`~app.clients.http.HttpAsrClient.plan_chunks` combines the injected prober
with :func:`compute_chunks` using ``self.chunk_length_seconds`` as the
threshold.

Zero-duration handling (documented decision)
--------------------------------------------
:func:`compute_chunks` returns an **empty list** for a non-positive duration
(``duration <= 0``). Rationale: a zero-length window carries no audio, so there
is nothing to submit to the ASR_Service. Task 5.2 therefore treats "no chunks"
as "no segments" and assembles an empty transcript rather than POSTing an empty
window. The union of the returned windows still exactly covers ``[0, duration]``
(which is the empty interval ``[0, 0]``), so Property 10 holds vacuously.
"""

from __future__ import annotations

import json
import subprocess

__all__ = ["compute_chunks", "parse_ffprobe_duration", "probe_duration"]


def compute_chunks(duration: float, threshold: int) -> list[tuple[float, float]]:
    """Split ``[0, duration]`` into sequential, non-overlapping windows.

    The returned windows satisfy, for any ``duration >= 0`` and any
    ``threshold > 0`` (Property 10 / Req 5.2):

    * **Contiguous & gap-free** -- ``chunks[0][0] == 0.0`` and
      ``chunks[i][1] == chunks[i + 1][0]`` for every adjacent pair.
    * **Non-overlapping** -- each window starts exactly where the previous ends.
    * **Exact coverage** -- ``chunks[-1][1] == duration`` (the union is exactly
      ``[0, duration]``).
    * **Bounded** -- every window's length is ``<= threshold``; only the final
      window may be shorter (the remainder).

    Behavior by regime:

    * ``duration <= 0`` -> ``[]`` (see module docstring; nothing to transcribe).
    * ``0 < duration <= threshold`` -> a single window ``[(0.0, duration)]``
      (chunking only subdivides when the duration *exceeds* the threshold, per
      Req 5.2).
    * ``duration > threshold`` -> ``ceil(duration / threshold)`` windows, each of
      length ``threshold`` except a possibly-shorter last one. Exact multiples
      of ``threshold`` produce equal full windows with **no** trailing empty
      window.

    :param duration: audio duration in seconds (from :func:`probe_duration`).
    :param threshold: maximum window length in seconds; the configured
        ``asr_chunk_length_seconds`` (validated to ``[30, 600]`` at startup,
        though this function accepts any positive integer).
    :returns: ordered list of ``(start_seconds, end_seconds)`` windows.
    :raises ValueError: if ``threshold <= 0``.
    """
    if threshold <= 0:
        raise ValueError(f"threshold must be positive, got {threshold!r}")

    if duration <= 0:
        return []

    duration = float(duration)
    if duration <= threshold:
        return [(0.0, duration)]

    chunks: list[tuple[float, float]] = []
    index = 0
    while True:
        # Compute boundaries from the integer index to avoid float drift across
        # many iterations (start is always an exact integer*threshold).
        start = float(index * threshold)
        if start >= duration:
            break
        end = min(float((index + 1) * threshold), duration)
        chunks.append((start, end))
        index += 1
    return chunks


def parse_ffprobe_duration(ffprobe_json: str) -> float:
    """Parse the duration (seconds) out of ``ffprobe -of json`` output.

    Expects the JSON payload produced by::

        ffprobe -v error -show_entries format=duration -of json <path>

    which looks like ``{"format": {"duration": "123.456"}}``. ffprobe reports
    the duration as a *string*, so it is coerced to ``float`` here.

    This is the testable seam for :func:`probe_duration`: unit tests can feed a
    captured payload without invoking the binary.

    :param ffprobe_json: the raw stdout from ffprobe.
    :returns: duration in seconds as a float.
    :raises ValueError: if the payload is not valid JSON, lacks a
        ``format.duration`` field, or that field is not a parseable number.
    """
    try:
        parsed = json.loads(ffprobe_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe output is not valid JSON: {exc}") from exc

    try:
        raw_duration = parsed["format"]["duration"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "ffprobe output is missing 'format.duration'"
        ) from exc

    try:
        return float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ffprobe duration is not a number: {raw_duration!r}"
        ) from exc


def probe_duration(audio_path: str, *, timeout: float = 30.0) -> float:
    """Return the duration of ``audio_path`` in seconds via ``ffprobe``.

    Shells out to ``ffprobe`` (part of the ffmpeg toolchain already required for
    preprocessing) and parses its JSON output with
    :func:`parse_ffprobe_duration`. This is the default ``duration_prober`` for
    :class:`~app.clients.http.HttpAsrClient`; tests inject a fake instead.

    :param audio_path: filesystem path to the (Standardized_Audio) file.
    :param timeout: hard wall-clock cap for the ffprobe subprocess in seconds.
    :returns: duration in seconds.
    :raises ValueError: if ffprobe is unavailable, exits non-zero, times out, or
        produces output that cannot be parsed into a duration.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        audio_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except FileNotFoundError as exc:  # ffprobe not installed / not on PATH
        raise ValueError("ffprobe executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ffprobe timed out after {timeout}s probing {audio_path!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"ffprobe failed for {audio_path!r}: {exc.stderr or exc}"
        ) from exc

    return parse_ffprobe_duration(completed.stdout)
