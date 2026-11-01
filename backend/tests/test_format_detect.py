"""Tests for content-based format detection -- task 8.2 (Req 3.2).

Two layers:

1. **Pure parse-step unit tests** over :func:`app.audio.format_detect.parse_ffprobe_format`
   using captured ffprobe ``-of json`` payloads (wav/mp3/m4a/webm -> canonical
   format; video-only / unknown / non-audio -> ``None``; garbled / missing JSON
   -> ``None``). No binary, no filesystem.
2. **Detector seam tests** over :func:`app.audio.format_detect.detect_audio_format`
   with an injected fake runner (and runners that raise the ffprobe failure
   modes) to confirm errors degrade to ``None``.

The endpoint-level 415/201 integration lives in
``tests/test_meetings_format_detect.py``. The optional real-ffprobe smoke test
below is skipped wherever the binary is absent (e.g. this dev env).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.audio.format_detect import detect_audio_format, parse_ffprobe_format


def _ffprobe_json(format_name: str, codec_names: list[str]) -> str:
    """Build an ffprobe ``-of json`` payload like the real tool emits."""
    return json.dumps(
        {
            "streams": [{"codec_name": c} for c in codec_names],
            "format": {"format_name": format_name},
        }
    )


# --- parse step: supported formats map to canonical strings -----------------


def test_parse_wav() -> None:
    payload = _ffprobe_json("wav", ["pcm_s16le"])
    assert parse_ffprobe_format(payload) == "wav"


def test_parse_mp3() -> None:
    payload = _ffprobe_json("mp3", ["mp3"])
    assert parse_ffprobe_format(payload) == "mp3"


def test_parse_m4a_from_mov_mp4_alias_list() -> None:
    # Real .m4a files report this comma-aliased container with an aac stream.
    payload = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", ["aac"])
    assert parse_ffprobe_format(payload) == "m4a"


def test_parse_m4a_with_alac_codec() -> None:
    payload = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", ["alac"])
    assert parse_ffprobe_format(payload) == "m4a"


def test_parse_m4a_with_cover_art_stream() -> None:
    # An mjpeg cover-art stream alongside real audio must still detect as m4a.
    payload = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", ["aac", "mjpeg"])
    assert parse_ffprobe_format(payload) == "m4a"


def test_parse_webm_from_matroska_alias() -> None:
    # Real .webm files report the matroska,webm alias with opus/vorbis audio.
    payload = _ffprobe_json("matroska,webm", ["opus"])
    assert parse_ffprobe_format(payload) == "webm"


def test_parse_webm_with_vorbis() -> None:
    payload = _ffprobe_json("matroska,webm", ["vorbis"])
    assert parse_ffprobe_format(payload) == "webm"


# --- parse step: unsupported / unknown / non-audio -> None ------------------


def test_parse_video_only_mp4_is_none() -> None:
    # mp4 container but only a video stream (no audio) -> not supported.
    payload = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", ["h264"])
    assert parse_ffprobe_format(payload) is None


def test_parse_video_only_webm_is_none() -> None:
    payload = _ffprobe_json("matroska,webm", ["vp9"])
    assert parse_ffprobe_format(payload) is None


def test_parse_unknown_container_is_none() -> None:
    payload = _ffprobe_json("flac", ["flac"])  # not in the supported set
    assert parse_ffprobe_format(payload) is None


def test_parse_image_is_none() -> None:
    payload = _ffprobe_json("image2", ["png"])
    assert parse_ffprobe_format(payload) is None


def test_parse_ogg_is_none() -> None:
    payload = _ffprobe_json("ogg", ["vorbis"])
    assert parse_ffprobe_format(payload) is None


def test_parse_mp4_no_streams_is_none() -> None:
    # Shared container with no streams at all -> cannot confirm audio.
    payload = _ffprobe_json("mov,mp4,m4a,3gp,3g2,mj2", [])
    assert parse_ffprobe_format(payload) is None


# --- parse step: malformed / missing payloads -> None -----------------------


def test_parse_garbled_json_is_none() -> None:
    assert parse_ffprobe_format("{not valid json") is None


def test_parse_empty_string_is_none() -> None:
    assert parse_ffprobe_format("") is None


def test_parse_missing_format_key_is_none() -> None:
    assert parse_ffprobe_format(json.dumps({"streams": [{"codec_name": "aac"}]})) is None


def test_parse_empty_format_name_is_none() -> None:
    payload = _ffprobe_json("", ["aac"])
    assert parse_ffprobe_format(payload) is None


def test_parse_non_object_json_is_none() -> None:
    assert parse_ffprobe_format(json.dumps([1, 2, 3])) is None


# --- detector seam: injected runner + failure degradation -------------------


def test_detect_uses_injected_runner() -> None:
    payload = _ffprobe_json("wav", ["pcm_s16le"])
    assert detect_audio_format("/tmp/x", runner=lambda _p: payload) == "wav"


def test_detect_missing_binary_returns_none() -> None:
    def _missing(_path: str) -> str:
        raise FileNotFoundError("ffprobe")

    assert detect_audio_format("/tmp/x", runner=_missing) is None


def test_detect_nonzero_exit_returns_none() -> None:
    def _failing(_path: str) -> str:
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr="bad input")

    assert detect_audio_format("/tmp/x", runner=_failing) is None


def test_detect_timeout_returns_none() -> None:
    def _timeout(_path: str) -> str:
        raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30.0)

    assert detect_audio_format("/tmp/x", runner=_timeout) is None


# --- optional: real ffprobe smoke (skipped when binary absent) --------------


@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe not installed in this environment"
)
def test_detect_real_wav_with_ffprobe(tmp_path) -> None:
    """Generate a tiny real WAV via ffmpeg and detect it via real ffprobe."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available to synthesize a test file")
    wav = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav),
        ],
        check=True,
        capture_output=True,
    )
    assert detect_audio_format(str(wav)) == "wav"
