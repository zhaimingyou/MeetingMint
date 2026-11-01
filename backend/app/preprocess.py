"""Audio_Preprocessor transcode step (task 7.1).

This module implements the single responsibility of the Audio_Preprocessor's
transcode stage: take an arbitrary Supported_Audio_Format source file and
produce **Standardized_Audio** -- single-channel (mono), 16 kHz, WAV
(``pcm_s16le``) -- by invoking ``ffmpeg`` as a subprocess under a **hard**
wall-clock timeout (``preprocess_timeout_seconds``, Req 4.3).

Scope (task 7.1)
----------------
Task 7.1 owns the *mechanism*: building the ffmpeg argument vector, running it
with a hard timeout, killing the child and cleaning up any partial output on
expiry, and cleaning up + surfacing a well-defined error on a non-zero exit
(decode/format failure). It is exposed as
:func:`transcode_to_standard_wav`.

Worker-facing failure handling (task 7.2)
-----------------------------------------
Task 7.2 adds the thin *categorization / description* layer the Celery worker
(task 9) consumes, without taking on task 9's responsibilities:

* :func:`preprocess_meeting_audio` -- the worker-facing entry point. On success
  it returns the persisted Standardized_Audio path (which the worker stores on
  the ``Meeting_Record`` and feeds to transcription, Req 4.4). On failure it
  re-raises the already-categorized exception with a guaranteed non-empty,
  recordable error description.
* :class:`PreprocessFailureCategory` + :func:`classify_preprocess_failure` /
  :func:`preprocess_failure_description` -- map a raised exception to a stable
  category label (``decode-error`` / ``timeout-error``) and a human-readable
  description string.

**Design boundary (do NOT cross here).** Writing ``Job_Status`` and persisting
``standardized_audio_path`` onto the ``Meeting_Record`` is owned by the worker
(task 9), which uses short transactions committed *before* each long call. This
module never touches the database. The contract the worker relies on is purely:

==========================  =====================================  ====================
Outcome                     This module                            Worker (task 9) maps to
==========================  =====================================  ====================
success                     returns ``output_path``                persist path, advance status
:class:`PreprocessTimeout`  raised; category ``timeout-error``     ``Job_Status = failed`` + timeout description
:class:`PreprocessError`    raised; category ``decode-error``      ``Job_Status = failed`` + decode description
==========================  =====================================  ====================

In every failure case the original source audio is **byte-for-byte unmodified**
and no Standardized_Audio output remains (the transcode only ever writes/cleans
``output_path`` and never opens the source for writing), satisfying Req 4.2/4.3.

Contract guarantees of :func:`transcode_to_standard_wav`:

* **Success** -- a valid mono / 16 kHz / WAV file is left at ``output_path`` and
  the path is returned.
* **Timeout** -- the ffmpeg child is killed (not left running), any partial
  ``output_path`` is removed, and :class:`PreprocessTimeout` is raised.
* **Non-zero exit** (undecodable / unsupported content) -- any partial
  ``output_path`` is removed and :class:`PreprocessError` is raised with the
  captured ffmpeg ``stderr`` included.
* **The source file is never modified or removed by this function** -- only the
  output path is ever written or cleaned up.

Injection seam (testability)
----------------------------
Because ffmpeg may not be installed in every environment, the subprocess call
is performed through an injectable ``runner`` callable (defaulting to
:func:`_default_runner`, a thin wrapper over ``subprocess.run``). The runner is
invoked as ``runner(cmd, timeout=timeout_seconds)`` and must either:

* return an object exposing ``returncode: int`` and ``stderr: str`` (mirroring
  :class:`subprocess.CompletedProcess`), or
* raise :class:`subprocess.TimeoutExpired` to signal the hard timeout fired.

Tests inject a fake runner to simulate success (creating the output file and
returning ``returncode=0``), a non-zero exit (``returncode!=0`` + stderr), or a
timeout (raising ``TimeoutExpired``) without needing a real ffmpeg binary. The
``ffmpeg_binary`` path is also parameterizable.

Why ``subprocess.run`` kills the child on timeout
-------------------------------------------------
The default runner uses :func:`subprocess.run`, which on
:class:`subprocess.TimeoutExpired` kills the spawned process and reaps it before
re-raising. So the "kill on expiry; child not left running" requirement is met
by the standard library for the real path; the fake-runner path simulates the
same exception so the cleanup branch is exercised in tests.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

__all__ = [
    "PreprocessError",
    "PreprocessTimeout",
    "PreprocessFailureCategory",
    "build_ffmpeg_command",
    "standardized_wav_path",
    "transcode_to_standard_wav",
    "preprocess_meeting_audio",
    "classify_preprocess_failure",
    "preprocess_failure_description",
]

# Standardized_Audio parameters (design Glossary: mono 16 kHz WAV).
STANDARD_CHANNELS = "1"
STANDARD_SAMPLE_RATE = "16000"
STANDARD_CODEC = "pcm_s16le"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class PreprocessError(Exception):
    """Terminal failure of the Audio_Preprocessor transcode step.

    Raised when ffmpeg exits non-zero (e.g. the source audio cannot be decoded
    or is in an unsupported/corrupt container) or cannot be launched. Task 7.2
    maps this to ``Job_Status = failed`` with a decode-error description while
    retaining the original source unmodified.

    :param message: human-readable error description recorded on the job.
    :param stderr: the captured ffmpeg ``stderr``, when available, for diagnosis.
    :param returncode: the ffmpeg process exit code, when available.
    """

    def __init__(
        self,
        message: str = "",
        *,
        stderr: str | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class PreprocessTimeout(PreprocessError):
    """The transcode did not finish within ``preprocess_timeout_seconds``.

    Subclasses :class:`PreprocessError` so callers that only care that
    preprocessing failed can catch the base type, while task 7.2 can catch
    :class:`PreprocessTimeout` explicitly to record the timeout-specific error
    description (Req 4.3). On this failure the ffmpeg child has been killed and
    any partial output removed.

    :param message: human-readable timeout description recorded on the job.
    :param timeout_seconds: the configured hard timeout that elapsed.
    """

    def __init__(self, message: str = "", *, timeout_seconds: float | None = None) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


# --------------------------------------------------------------------------- #
# Runner seam
# --------------------------------------------------------------------------- #
class _RunResult(Protocol):
    """Structural type of what a ``runner`` returns (subset of CompletedProcess)."""

    returncode: int
    stderr: str


# A runner takes the ffmpeg argv and a hard timeout (seconds) and either returns
# a result with returncode/stderr or raises subprocess.TimeoutExpired.
Runner = Callable[..., _RunResult]


def _default_runner(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Default ``runner``: run ffmpeg, capturing output, under a hard timeout.

    ``check=False`` so a non-zero exit is returned (not raised) and handled
    uniformly by :func:`transcode_to_standard_wav`. On timeout
    :func:`subprocess.run` kills the child and re-raises
    :class:`subprocess.TimeoutExpired`, which the caller turns into a
    :class:`PreprocessTimeout`.
    """
    return subprocess.run(  # noqa: S603 - cmd is built from validated args, not shell
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Command construction (the pinned contract)
# --------------------------------------------------------------------------- #
def build_ffmpeg_command(
    source_path: str,
    output_path: str,
    *,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    """Build the ffmpeg argument vector that produces Standardized_Audio.

    The vector encodes the full transcode contract so it can be asserted in
    tests without running ffmpeg:

    * ``-nostdin`` -- never block reading from stdin.
    * ``-v error`` -- quiet logging; ``stderr`` carries only real errors.
    * ``-y`` -- overwrite ``output_path`` if it already exists.
    * ``-i <source_path>`` -- the input file (read-only; never modified).
    * ``-vn`` -- drop any video stream (e.g. cover art / webm video track).
    * ``-ac 1`` -- downmix to a single (mono) channel.
    * ``-ar 16000`` -- resample to 16 kHz.
    * ``-c:a pcm_s16le`` / ``-f wav`` -- 16-bit PCM WAV output.
    * ``<output_path>`` -- the Standardized_Audio destination.

    :param source_path: path to the source audio (any Supported_Audio_Format).
    :param output_path: destination path for the mono 16 kHz WAV.
    :param ffmpeg_binary: ffmpeg executable name or absolute path (parameterized
        for tests / non-standard installs).
    :returns: the argv list suitable for :func:`subprocess.run`.
    """
    return [
        ffmpeg_binary,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        source_path,
        "-vn",
        "-ac",
        STANDARD_CHANNELS,
        "-ar",
        STANDARD_SAMPLE_RATE,
        "-c:a",
        STANDARD_CODEC,
        "-f",
        "wav",
        output_path,
    ]


def standardized_wav_path(audio_storage_dir: str, meeting_id: str) -> str:
    """Compute a conventional Standardized_Audio output path for a meeting.

    Convenience helper for the worker (task 9): the standardized WAV for a
    ``Meeting_Record`` lives under ``audio_storage_dir`` named by its id. The
    transcode function itself takes an explicit ``output_path`` and does not
    depend on this helper, so the worker is free to choose another location.

    :param audio_storage_dir: the configured ``audio_storage_dir``.
    :param meeting_id: the Meeting_Record identifier.
    :returns: ``<audio_storage_dir>/<meeting_id>.standardized.wav``.
    """
    return os.path.join(audio_storage_dir, f"{meeting_id}.standardized.wav")


# --------------------------------------------------------------------------- #
# Transcode
# --------------------------------------------------------------------------- #
def _remove_partial_output(output_path: str) -> None:
    """Best-effort removal of a (possibly partial) output file.

    Used on timeout and non-zero exit so a half-written WAV is never left
    behind. Never raises: a missing file is fine, and an unexpected OS error
    while cleaning up must not mask the original failure being reported.
    """
    try:
        os.remove(output_path)
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - defensive; cleanup must not mask errors
        pass


def transcode_to_standard_wav(
    source_path: str,
    output_path: str,
    *,
    timeout_seconds: int,
    ffmpeg_binary: str = "ffmpeg",
    runner: Runner | None = None,
) -> str:
    """Transcode ``source_path`` to a mono 16 kHz WAV at ``output_path``.

    Invokes ffmpeg (via the injectable ``runner``) with the argument vector from
    :func:`build_ffmpeg_command` under a **hard** ``timeout_seconds`` cap. The
    source file is only ever read; it is never modified or removed by this
    function.

    :param source_path: path to the source audio (any Supported_Audio_Format).
    :param output_path: destination path for the Standardized_Audio WAV.
    :param timeout_seconds: hard wall-clock timeout; on expiry the ffmpeg child
        is killed and any partial output removed (Req 4.3).
    :param ffmpeg_binary: ffmpeg executable name or path (parameterized for
        tests / non-standard installs).
    :param runner: optional subprocess runner seam; defaults to
        :func:`_default_runner`. Invoked as ``runner(cmd, timeout=...)`` and
        must return an object with ``returncode``/``stderr`` or raise
        :class:`subprocess.TimeoutExpired`.
    :returns: ``output_path`` on success.
    :raises PreprocessTimeout: if the transcode exceeds ``timeout_seconds``.
    :raises PreprocessError: if ffmpeg cannot be launched or exits non-zero
        (e.g. undecodable / unsupported source).
    """
    run = runner or _default_runner
    cmd = build_ffmpeg_command(source_path, output_path, ffmpeg_binary=ffmpeg_binary)

    try:
        result = run(cmd, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed and reaped the child; ensure no
        # partial WAV is left behind for the next stage to read.
        _remove_partial_output(output_path)
        raise PreprocessTimeout(
            f"ffmpeg transcode timed out after {timeout_seconds}s for {source_path!r}",
            timeout_seconds=timeout_seconds,
        ) from exc
    except FileNotFoundError as exc:
        # ffmpeg binary missing / not on PATH. No output could have been
        # created, but clean up defensively and surface a uniform error type.
        _remove_partial_output(output_path)
        raise PreprocessError(
            f"ffmpeg executable not found: {ffmpeg_binary!r}"
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        _remove_partial_output(output_path)
        raise PreprocessError(
            f"ffmpeg failed to transcode {source_path!r} "
            f"(exit code {result.returncode}): {stderr}",
            stderr=stderr or None,
            returncode=result.returncode,
        )

    return output_path


# --------------------------------------------------------------------------- #
# Worker-facing failure handling (task 7.2)
# --------------------------------------------------------------------------- #
class PreprocessFailureCategory(StrEnum):
    """Stable category label for a preprocessing failure (Req 4.2, 4.3).

    The Celery worker (task 9) uses this to decide which error description to
    record alongside ``Job_Status = failed``. The value strings are part of the
    contract and safe to surface to operators / logs:

    * ``DECODE_ERROR`` -- ffmpeg could not decode the source (undecodable /
      unsupported / corrupt) or could not be launched (Req 4.2).
    * ``TIMEOUT_ERROR`` -- the transcode exceeded ``preprocess_timeout_seconds``
      and was terminated (Req 4.3).
    """

    DECODE_ERROR = "decode-error"
    TIMEOUT_ERROR = "timeout-error"


def classify_preprocess_failure(exc: PreprocessError) -> PreprocessFailureCategory:
    """Map a raised preprocessing exception to its :class:`PreprocessFailureCategory`.

    The classification is driven purely by the exception *type*, which is the
    same signal task 9 keys off:

    * :class:`PreprocessTimeout` -> :attr:`PreprocessFailureCategory.TIMEOUT_ERROR`
    * any other :class:`PreprocessError` -> :attr:`PreprocessFailureCategory.DECODE_ERROR`

    :param exc: the exception raised by :func:`preprocess_meeting_audio` /
        :func:`transcode_to_standard_wav`.
    :returns: the matching category label.
    """
    if isinstance(exc, PreprocessTimeout):
        return PreprocessFailureCategory.TIMEOUT_ERROR
    return PreprocessFailureCategory.DECODE_ERROR


def preprocess_failure_description(exc: PreprocessError) -> str:
    """Return a guaranteed non-empty, recordable error description for ``exc``.

    Normally this is just ``str(exc)`` (the transcode step always raises with a
    descriptive message that, for decode failures, includes the captured ffmpeg
    ``stderr`` and, for timeouts, includes the elapsed timeout). As a safety
    net, if the exception carries no message this synthesizes a category-derived
    description so the worker never records an empty error string (Req 4.2/4.3).

    :param exc: the raised preprocessing exception.
    :returns: a non-empty human-readable description suitable for persisting on
        the failed job.
    """
    text = str(exc).strip()
    if text:
        return text

    category = classify_preprocess_failure(exc)
    if category is PreprocessFailureCategory.TIMEOUT_ERROR:
        seconds = getattr(exc, "timeout_seconds", None)
        if seconds is not None:
            return f"audio preprocessing timed out after {seconds}s"
        return "audio preprocessing timed out"
    return "audio preprocessing failed: source audio could not be decoded"


def preprocess_meeting_audio(
    source_path: str,
    output_path: str,
    *,
    timeout_seconds: int,
    ffmpeg_binary: str = "ffmpeg",
    runner: Runner | None = None,
) -> str:
    """Worker-facing preprocessing step: transcode to Standardized_Audio.

    Thin wrapper over :func:`transcode_to_standard_wav` that defines the clean
    outcome contract the Celery worker (task 9) maps to ``Job_Status``:

    * **Success** -- returns ``output_path``, the persisted Standardized_Audio
      (mono 16 kHz WAV) the worker records on the ``Meeting_Record`` and feeds
      to transcription (Req 4.4).
    * **Timeout** -- re-raises :class:`PreprocessTimeout` (category
      ``timeout-error``) carrying a non-empty description that includes the
      elapsed timeout (Req 4.3).
    * **Decode / launch failure** -- re-raises :class:`PreprocessError`
      (category ``decode-error``) carrying a non-empty description (Req 4.2).

    Use :func:`classify_preprocess_failure` and
    :func:`preprocess_failure_description` on the caught exception to obtain the
    category label and the description to persist.

    **Original-source guarantee.** This function never opens ``source_path`` for
    writing and never removes it; the underlying transcode only ever writes to
    ``output_path`` and, on any failure, removes the (possibly partial) output.
    Therefore on every code path the original source audio is left byte-for-byte
    unmodified and no Standardized_Audio remains after a failure (Req 4.2/4.3).

    **Boundary.** This function does NOT write ``Job_Status`` or persist the
    standardized path to the database -- that is the worker's responsibility
    (task 9), kept here as a pure, side-effect-light function over the
    filesystem.

    :param source_path: path to the source audio (any Supported_Audio_Format);
        read-only.
    :param output_path: destination path for the Standardized_Audio WAV.
    :param timeout_seconds: hard wall-clock timeout (``preprocess_timeout_seconds``).
    :param ffmpeg_binary: ffmpeg executable name or path.
    :param runner: optional subprocess runner seam (see
        :func:`transcode_to_standard_wav`).
    :returns: ``output_path`` on success.
    :raises PreprocessTimeout: on transcode timeout (category ``timeout-error``).
    :raises PreprocessError: on decode / launch failure (category ``decode-error``).
    """
    try:
        return transcode_to_standard_wav(
            source_path,
            output_path,
            timeout_seconds=timeout_seconds,
            ffmpeg_binary=ffmpeg_binary,
            runner=runner,
        )
    except PreprocessError as exc:
        # Preserve the already-categorized exception type (PreprocessTimeout vs
        # PreprocessError) that task 9 dispatches on, but guarantee a non-empty,
        # recordable description regardless of how it was constructed.
        if not str(exc).strip():
            exc.args = (preprocess_failure_description(exc),)
        raise
