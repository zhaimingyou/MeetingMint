"""Example/edge unit tests for the Audio_Preprocessor transcode step (task 7.1).

These tests pin the ffmpeg argument-vector contract and exercise the three
outcome branches of :func:`app.preprocess.transcode_to_standard_wav` -- success,
hard timeout, and non-zero exit -- through the injectable ``runner`` seam, so
they run without a real ffmpeg binary. A single integration-style test that
actually transcodes is guarded by ``shutil.which("ffmpeg")`` and skips when the
binary is absent.

The Property 8/9 hypothesis tests (normalize-to-mono-16k, undecodable preserves
original) are task 7.3; these are the focused examples for task 7.1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave

import pytest

from app.preprocess import (
    PreprocessError,
    PreprocessTimeout,
    build_ffmpeg_command,
    standardized_wav_path,
    transcode_to_standard_wav,
)


# --------------------------------------------------------------------------- #
# build_ffmpeg_command: pin the transcode contract
# --------------------------------------------------------------------------- #
def test_command_contains_mono_16k_wav_and_paths():
    cmd = build_ffmpeg_command("/in/source.m4a", "/out/std.wav")
    # mono
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
    # 16 kHz
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
    # WAV output format
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "wav"
    # input via -i, source and output paths present in order
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "/in/source.m4a"
    assert cmd[-1] == "/out/std.wav"
    # overwrite, drop video, quiet, no stdin
    assert "-y" in cmd
    assert "-vn" in cmd
    assert "-nostdin" in cmd


def test_command_respects_custom_binary():
    cmd = build_ffmpeg_command("a.mp3", "b.wav", ffmpeg_binary="/opt/ffmpeg")
    assert cmd[0] == "/opt/ffmpeg"


def test_standardized_wav_path():
    assert standardized_wav_path("/data/audio", "abc-123") == "/data/audio/abc-123.standardized.wav"


# --------------------------------------------------------------------------- #
# Fake runners for the injection seam
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _make_success_runner(output_path: str):
    """Runner that simulates ffmpeg writing the output and exiting 0."""
    captured: dict = {}

    def _runner(cmd, *, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        with open(output_path, "wb") as fh:
            fh.write(b"RIFFfake-wav-bytes")
        return _FakeResult(returncode=0)

    return _runner, captured


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #
def test_success_leaves_output_and_does_not_touch_source(tmp_path):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"

    runner, captured = _make_success_runner(str(output))
    result = transcode_to_standard_wav(
        str(source), str(output), timeout_seconds=600, runner=runner
    )

    assert result == str(output)
    assert output.exists()
    # source untouched (byte-for-byte)
    assert source.read_bytes() == source_digest
    # the configured timeout is propagated to the runner
    assert captured["timeout"] == 600
    # contract pinned: argv carries mono/16k and both paths
    cmd = captured["cmd"]
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert str(source) in cmd
    assert cmd[-1] == str(output)


# --------------------------------------------------------------------------- #
# Timeout path
# --------------------------------------------------------------------------- #
def test_timeout_kills_cleans_partial_and_raises(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"
    # Simulate a partially written output that must be cleaned up on timeout.
    output.write_bytes(b"partial")

    def _timeout_runner(cmd, *, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with pytest.raises(PreprocessTimeout) as excinfo:
        transcode_to_standard_wav(
            str(source), str(output), timeout_seconds=1, runner=_timeout_runner
        )

    assert excinfo.value.timeout_seconds == 1
    # partial output removed
    assert not output.exists()
    # source untouched
    assert source.read_bytes() == source_digest


# --------------------------------------------------------------------------- #
# Non-zero exit path (decode/format failure)
# --------------------------------------------------------------------------- #
def test_nonzero_exit_cleans_partial_and_raises_with_stderr(tmp_path):
    source = tmp_path / "source.webm"
    source.write_bytes(b"original-source-bytes")
    source_digest = source.read_bytes()
    output = tmp_path / "std.wav"
    output.write_bytes(b"partial")

    def _fail_runner(cmd, *, timeout):
        return _FakeResult(returncode=1, stderr="moov atom not found")

    with pytest.raises(PreprocessError) as excinfo:
        transcode_to_standard_wav(
            str(source), str(output), timeout_seconds=600, runner=_fail_runner
        )

    # stderr surfaced for diagnosis (task 7.2 maps to decode-error description)
    assert "moov atom not found" in str(excinfo.value)
    assert excinfo.value.stderr == "moov atom not found"
    assert excinfo.value.returncode == 1
    # partial output removed; source untouched
    assert not output.exists()
    assert source.read_bytes() == source_digest


def test_timeout_is_a_preprocess_error_subclass():
    # 7.2 may catch the base type for the generic "preprocessing failed" path.
    assert issubclass(PreprocessTimeout, PreprocessError)


def test_missing_ffmpeg_binary_raises_preprocess_error(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"bytes")
    output = tmp_path / "std.wav"

    def _missing_runner(cmd, *, timeout):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(PreprocessError):
        transcode_to_standard_wav(
            str(source), str(output), timeout_seconds=600, runner=_missing_runner
        )


# --------------------------------------------------------------------------- #
# Optional integration test against a real ffmpeg (skipped when absent)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_transcodes_to_mono_16k(tmp_path):
    # Generate a tiny stereo 44.1 kHz WAV source with ffmpeg's sine generator.
    source = tmp_path / "source.wav"
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

    transcode_to_standard_wav(str(source), str(output), timeout_seconds=60)

    assert output.exists()
    with wave.open(str(output), "rb") as wf:
        assert wf.getnchannels() == 1  # mono
        assert wf.getframerate() == 16000  # 16 kHz
    # source untouched
    assert source.read_bytes() == source_digest
    assert os.path.getsize(str(source)) == len(source_digest)
