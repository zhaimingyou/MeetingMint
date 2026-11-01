"""Property-based tests for the Audio_Preprocessor (task 7.3).

This module holds the ``hypothesis`` property tests (>= 100 examples each) for
the two design properties owned by the preprocessing step, plus the unit test
for hard-timeout termination, without requiring a real ffmpeg binary. All
subprocess I/O is exercised through the injectable ``runner`` seam of
:func:`app.preprocess.preprocess_meeting_audio` (a fake ffmpeg), so the tests
are deterministic and run in any environment:

* **Property 8** -- preprocessing normalizes any decodable source to mono
  16 kHz WAV, persisted and referenced as the transcription input
  (Req 4.1, 4.4). Verified at the contract/seam level: the built ffmpeg argv
  *always* encodes mono / 16 kHz / wav regardless of the (diverse) input, and a
  simulated-success run returns and persists the standardized ``output_path``
  while never writing or removing the source.
* **Property 9** -- an undecodable source fails the job (decode-error category,
  non-empty description) and preserves the original byte-for-byte (Req 4.2).

The example/edge tests for the same code paths live in ``test_preprocess.py``
(task 7.1) and ``test_preprocess_failure.py`` (task 7.2); this module adds only
the universal-property coverage and the forced-timeout unit test, and does not
duplicate those examples.

A single real-ffmpeg integration check for Property 8 is guarded by
``shutil.which("ffmpeg")`` and skips where the binary is absent (e.g. this dev
environment); it runs wherever ffmpeg is installed and confirms the produced
WAV is genuinely mono + 16 kHz via the stdlib ``wave`` module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.preprocess import (
    STANDARD_CHANNELS,
    STANDARD_SAMPLE_RATE,
    PreprocessError,
    PreprocessFailureCategory,
    PreprocessTimeout,
    build_ffmpeg_command,
    classify_preprocess_failure,
    preprocess_failure_description,
    preprocess_meeting_audio,
)


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
# The Supported_Audio_Format extensions (design Glossary): mp3, wav, m4a, webm.
_SUPPORTED_EXTENSIONS = st.sampled_from(["mp3", "wav", "m4a", "webm"])

# Diverse, filesystem-safe base names including unicode and spaces. The
# alphabet is a curated mix of ASCII letters/digits/space and assigned CJK
# characters (Chinese meetings) so names are genuinely varied + non-ASCII while
# always being valid, encodable single path components -- arbitrary
# `st.characters` ranges can emit unassigned codepoints that the OS rejects with
# EILSEQ when used as a filename, which would test the filesystem, not the code.
_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_"
    "会议录音备份测试音频文件名"
)
_BASE_NAMES = (
    st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=24)
    .map(lambda s: s.strip())
    .filter(lambda s: s not in {"", ".", ".."})
)


class _FakeResult:
    """Subset of :class:`subprocess.CompletedProcess` the runner seam returns."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _success_runner(output_path: str, captured: dict):
    """Fake ffmpeg that simulates success: writes ``output_path``, exits 0.

    Records the argv and timeout it was invoked with so the test can assert the
    normalize-to-mono-16k-WAV contract was carried on the actual command, for
    every generated input.
    """

    def _runner(cmd, *, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        with open(output_path, "wb") as fh:
            fh.write(b"RIFFfake-standardized-wav")
        return _FakeResult(returncode=0)

    return _runner


# --------------------------------------------------------------------------- #
# Property 8
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 8: Preprocessing normalizes any decodable source to mono 16 kHz WAV
# Validates: Requirements 4.1, 4.4
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    src_name=_BASE_NAMES,
    src_ext=_SUPPORTED_EXTENSIONS,
    out_name=_BASE_NAMES,
    timeout_seconds=st.integers(min_value=1, max_value=600),
)
def test_property_8_normalizes_to_mono_16k_wav(
    tmp_path_factory, src_name, src_ext, out_name, timeout_seconds
):
    # Fresh isolated directory per example (function-scoped tmp_path can't be
    # used directly with @given).
    work = tmp_path_factory.mktemp("p8")
    source = work / f"{src_name}.{src_ext}"
    source.write_bytes(b"decodable-source-bytes")
    source_digest = source.read_bytes()
    source_stat = source.stat()
    output = work / f"{out_name}.standardized.wav"

    captured: dict = {}
    result = preprocess_meeting_audio(
        str(source),
        str(output),
        timeout_seconds=timeout_seconds,
        runner=_success_runner(str(output), captured),
    )

    # The persisted Standardized_Audio path is returned -- this is what the
    # worker records and feeds to transcription (Req 4.4).
    assert result == str(output)
    assert output.exists()

    # The built ffmpeg argv ALWAYS encodes mono / 16 kHz / wav regardless of the
    # input -- the "normalize to mono 16k WAV" guarantee (Req 4.1).
    cmd = captured["cmd"]
    assert cmd[cmd.index("-ac") + 1] == STANDARD_CHANNELS == "1"
    assert cmd[cmd.index("-ar") + 1] == STANDARD_SAMPLE_RATE == "16000"
    assert cmd[cmd.index("-f") + 1] == "wav"
    # The source is passed as the ffmpeg input and the standardized file is the
    # output target.
    assert cmd[cmd.index("-i") + 1] == str(source)
    assert cmd[-1] == str(output)
    # The configured hard timeout is propagated to the runner (Req 4.3 cap).
    assert captured["timeout"] == timeout_seconds

    # The source is never written or removed: byte-for-byte identical and same
    # inode/mtime after the call.
    assert source.exists()
    assert source.read_bytes() == source_digest
    after_stat = source.stat()
    assert after_stat.st_size == source_stat.st_size
    assert after_stat.st_mtime_ns == source_stat.st_mtime_ns


# Feature: meeting-minutes-service, Property 8: Preprocessing normalizes any decodable source to mono 16 kHz WAV
# Validates: Requirements 4.1, 4.4
def test_property_8_build_command_pins_mono_16k_wav_for_any_paths():
    # Pure-contract check of the argv builder across a couple of representative
    # inputs (the @given test exercises the full integrated path); kept here to
    # document that the normalization flags are independent of input format.
    for src in ("/in/会议.mp3", "/in/a b.webm", "/in/x.m4a", "/in/y.wav"):
        cmd = build_ffmpeg_command(src, "/out/std.wav")
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-ar") + 1] == "16000"
        assert cmd[cmd.index("-f") + 1] == "wav"


# --------------------------------------------------------------------------- #
# Property 8: real-ffmpeg integration (skips when ffmpeg is unavailable)
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 8: Preprocessing normalizes any decodable source to mono 16 kHz WAV
# Validates: Requirements 4.1, 4.4
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_property_8_real_ffmpeg_produces_mono_16k_wav(tmp_path):
    # Generate a tiny stereo 44.1 kHz source via ffmpeg's sine generator, then
    # confirm the real transcode actually yields mono + 16 kHz WAV.
    source = tmp_path / "源.wav"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ac", "2", "-ar", "44100", str(source),
        ],
        check=True,
    )
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"

    result = preprocess_meeting_audio(str(source), str(output), timeout_seconds=60)

    assert result == str(output)
    assert output.exists()
    with wave.open(str(output), "rb") as wf:
        assert wf.getnchannels() == 1  # mono (Req 4.1)
        assert wf.getframerate() == 16000  # 16 kHz (Req 4.1)
    # Original source preserved byte-for-byte.
    assert source.read_bytes() == source_digest


# --------------------------------------------------------------------------- #
# Property 9
# --------------------------------------------------------------------------- #
# Feature: meeting-minutes-service, Property 9: Undecodable source fails the job and preserves the original
# Validates: Requirements 4.2
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    source_bytes=st.binary(min_size=0, max_size=256),
    stderr_text=st.text(max_size=80),
    return_code=st.integers(min_value=1, max_value=255),
    leave_partial=st.booleans(),
)
def test_property_9_undecodable_fails_and_preserves_original(
    tmp_path_factory, source_bytes, stderr_text, return_code, leave_partial
):
    work = tmp_path_factory.mktemp("p9")
    source = work / "source.bin"
    source.write_bytes(source_bytes)
    source_digest = source.read_bytes()
    source_stat = source.stat()
    output = work / "std.wav"
    # Optionally simulate a partial output the failed transcode must clean up.
    if leave_partial:
        output.write_bytes(b"partial-wav-bytes")

    def _decode_fail_runner(cmd, *, timeout):
        # ffmpeg could not decode the source -> non-zero exit with stderr.
        return _FakeResult(returncode=return_code, stderr=stderr_text)

    with pytest.raises(PreprocessError) as excinfo:
        preprocess_meeting_audio(
            str(source), str(output), timeout_seconds=600, runner=_decode_fail_runner
        )

    exc = excinfo.value
    # A decode failure, not a timeout -> decode-error category (Req 4.2).
    assert not isinstance(exc, PreprocessTimeout)
    assert classify_preprocess_failure(exc) is PreprocessFailureCategory.DECODE_ERROR
    # A non-empty, recordable decode-error description is available for the job.
    description = preprocess_failure_description(exc)
    assert description.strip()

    # No Standardized_Audio output is left behind.
    assert not output.exists()

    # The original source audio remains byte-for-byte unmodified.
    assert source.exists()
    assert source.read_bytes() == source_digest
    after_stat = source.stat()
    assert after_stat.st_size == source_stat.st_size
    assert after_stat.st_mtime_ns == source_stat.st_mtime_ns


# --------------------------------------------------------------------------- #
# Unit test: timeout termination with a forced tiny timeout (Req 4.3)
# --------------------------------------------------------------------------- #
def test_forced_tiny_timeout_terminates_and_preserves_source(tmp_path):
    """A forced tiny timeout terminates the transcode and preserves the source.

    Uses a runner that raises :class:`subprocess.TimeoutExpired` to simulate the
    hard ``preprocess_timeout_seconds`` cap firing (the real ``subprocess.run``
    kills + reaps the child on timeout; we cannot rely on a real ffmpeg here).
    Asserts the failure is a :class:`PreprocessTimeout` (timeout-error category)
    with a non-empty description, no standardized output remains, and the source
    is byte-for-byte unchanged.
    """
    source = tmp_path / "源音频.m4a"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"
    # A partial output that the timeout cleanup must remove.
    output.write_bytes(b"partial")

    tiny_timeout = 1
    seen: dict = {}

    def _timeout_runner(cmd, *, timeout):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with pytest.raises(PreprocessTimeout) as excinfo:
        preprocess_meeting_audio(
            str(source),
            str(output),
            timeout_seconds=tiny_timeout,
            runner=_timeout_runner,
        )

    exc = excinfo.value
    # The forced tiny timeout was the cap handed to the (would-be) ffmpeg run.
    assert seen["timeout"] == tiny_timeout
    assert exc.timeout_seconds == tiny_timeout
    # timeout-error category with a non-empty, recordable description (Req 4.3).
    assert classify_preprocess_failure(exc) is PreprocessFailureCategory.TIMEOUT_ERROR
    description = preprocess_failure_description(exc)
    assert description.strip()
    assert str(tiny_timeout) in description
    # No standardized output left behind; source untouched.
    assert not output.exists()
    assert source.read_bytes() == source_digest
    assert os.path.getsize(str(source)) == len(source_digest)
