"""Example/edge unit tests for the worker-facing preprocessing layer (task 7.2).

These tests pin the *contract* the Celery worker (task 9) relies on when it
calls :func:`app.preprocess.preprocess_meeting_audio`:

* success returns the Standardized_Audio ``output_path`` and leaves the source
  byte-for-byte unchanged (Req 4.4);
* an undecodable / non-zero-exit source fails with a non-empty ``decode-error``
  description, leaves no standardized output, and preserves the original source
  byte-for-byte (Req 4.2);
* a timeout fails with a non-empty ``timeout-error`` description that mentions
  the elapsed seconds, leaves no output, and preserves the source (Req 4.3).

They also verify the categorization helpers
(:func:`classify_preprocess_failure` / :func:`preprocess_failure_description`)
that map an exception type -> category label + recordable description, which is
exactly what task 9 will use.

The Property 8/9 hypothesis tests are task 7.3; these are the focused examples
for task 7.2 and run without a real ffmpeg binary via the injectable runner.
"""

from __future__ import annotations

import subprocess

import pytest

from app.preprocess import (
    PreprocessError,
    PreprocessFailureCategory,
    PreprocessTimeout,
    classify_preprocess_failure,
    preprocess_failure_description,
    preprocess_meeting_audio,
)


# --------------------------------------------------------------------------- #
# Fake runners for the injection seam
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _success_runner(output_path: str):
    def _runner(cmd, *, timeout):
        with open(output_path, "wb") as fh:
            fh.write(b"RIFFfake-wav-bytes")
        return _FakeResult(returncode=0)

    return _runner


# --------------------------------------------------------------------------- #
# Success path: returns standardized path, source untouched (Req 4.4)
# --------------------------------------------------------------------------- #
def test_success_returns_output_path_and_preserves_source(tmp_path):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"

    result = preprocess_meeting_audio(
        str(source),
        str(output),
        timeout_seconds=600,
        runner=_success_runner(str(output)),
    )

    # The worker persists this path on the Meeting_Record / uses it as the
    # transcription input.
    assert result == str(output)
    assert output.exists()
    # Source is byte-for-byte unchanged.
    assert source.read_bytes() == source_digest


# --------------------------------------------------------------------------- #
# Decode failure: decode-error category, description, no output, source intact
# --------------------------------------------------------------------------- #
def test_decode_failure_categorized_and_preserves_source(tmp_path):
    source = tmp_path / "source.webm"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"
    # A partial output that must not survive the failure.
    output.write_bytes(b"partial")

    def _fail_runner(cmd, *, timeout):
        return _FakeResult(returncode=1, stderr="Invalid data found when processing input")

    with pytest.raises(PreprocessError) as excinfo:
        preprocess_meeting_audio(
            str(source), str(output), timeout_seconds=600, runner=_fail_runner
        )

    exc = excinfo.value
    # Not a timeout -> decode-error category.
    assert not isinstance(exc, PreprocessTimeout)
    assert classify_preprocess_failure(exc) is PreprocessFailureCategory.DECODE_ERROR
    # Recordable, non-empty description that mentions the failure.
    description = preprocess_failure_description(exc)
    assert description
    assert "Invalid data found when processing input" in description
    # No standardized output left behind.
    assert not output.exists()
    # Original source byte-for-byte unchanged.
    assert source.read_bytes() == source_digest


def test_missing_binary_is_decode_error_category(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"

    def _missing_runner(cmd, *, timeout):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(PreprocessError) as excinfo:
        preprocess_meeting_audio(
            str(source), str(output), timeout_seconds=600, runner=_missing_runner
        )

    exc = excinfo.value
    assert classify_preprocess_failure(exc) is PreprocessFailureCategory.DECODE_ERROR
    assert preprocess_failure_description(exc)
    assert not output.exists()
    assert source.read_bytes() == source_digest


# --------------------------------------------------------------------------- #
# Timeout failure: timeout-error category, description mentions seconds
# --------------------------------------------------------------------------- #
def test_timeout_failure_categorized_and_preserves_source(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"
    output.write_bytes(b"partial")

    def _timeout_runner(cmd, *, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with pytest.raises(PreprocessTimeout) as excinfo:
        preprocess_meeting_audio(
            str(source), str(output), timeout_seconds=5, runner=_timeout_runner
        )

    exc = excinfo.value
    assert classify_preprocess_failure(exc) is PreprocessFailureCategory.TIMEOUT_ERROR
    # Description mentions the timeout / elapsed seconds.
    description = preprocess_failure_description(exc)
    assert description
    assert "5" in description
    assert "timed out" in description.lower() or "timeout" in description.lower()
    assert exc.timeout_seconds == 5
    # No output, source untouched.
    assert not output.exists()
    assert source.read_bytes() == source_digest


# --------------------------------------------------------------------------- #
# Categorization helper contract (what task 9 dispatches on)
# --------------------------------------------------------------------------- #
def test_classify_distinguishes_timeout_from_decode():
    assert (
        classify_preprocess_failure(PreprocessTimeout("x", timeout_seconds=1))
        is PreprocessFailureCategory.TIMEOUT_ERROR
    )
    assert (
        classify_preprocess_failure(PreprocessError("x"))
        is PreprocessFailureCategory.DECODE_ERROR
    )


def test_description_synthesized_when_message_empty():
    # Safety net: even an empty-message exception yields a non-empty,
    # category-appropriate description for the worker to record.
    decode_desc = preprocess_failure_description(PreprocessError(""))
    assert decode_desc
    assert "decode" in decode_desc.lower()

    timeout_desc = preprocess_failure_description(PreprocessTimeout("", timeout_seconds=42))
    assert timeout_desc
    assert "42" in timeout_desc


def test_failure_category_values_are_stable_labels():
    assert PreprocessFailureCategory.DECODE_ERROR.value == "decode-error"
    assert PreprocessFailureCategory.TIMEOUT_ERROR.value == "timeout-error"
