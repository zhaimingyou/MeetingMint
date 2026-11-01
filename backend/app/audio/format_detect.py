"""Content-based Supported_Audio_Format detection via ffprobe (task 8.2).

The upload endpoint (``POST /api/meetings``) must decide whether an uploaded
file is a Supported_Audio_Format (``mp3`` / ``wav`` / ``m4a`` / ``webm``) and
reject anything else with **415** *before* a ``Meeting_Record`` is created
(Req 3.2). Crucially the decision is made by **inspecting the file's actual
content** with ``ffprobe`` -- never by trusting the filename or extension --
because browser-recorded blobs arrive with no reliable name (design:
"detects format by content inspection via ffprobe rather than filename/
extension").

What this module provides
-------------------------
* :func:`detect_audio_format` -- run ffprobe over a file and map the result to
  one of the supported canonical format strings, or ``None`` when the content
  cannot be mapped to a supported format (unsupported / unknown / non-audio /
  unreadable). This is what the endpoint calls.
* :func:`parse_ffprobe_format` -- the **pure** parse + mapping step over the
  ffprobe JSON payload. Factored out so the mapping rules can be unit-tested
  directly against captured ffprobe output without invoking the binary.

ffprobe invocation
------------------
Mirrors the existing ffprobe usage in :mod:`app.clients.chunking`::

    ffprobe -v error \
        -show_entries format=format_name \
        -show_entries stream=codec_name \
        -of json <path>

which yields, for example::

    {"streams": [{"codec_name": "pcm_s16le"}], "format": {"format_name": "wav"}}

The container ``format_name`` is frequently a **comma-separated alias list**
(e.g. ``"mov,mp4,m4a,3gp,3g2,mj2"`` for an ``.m4a``; ``"matroska,webm"`` for a
``.webm``), so the mapping tokenizes it and matches against the supported set.

Mapping rules (ffprobe ``format_name``/``codec_name`` -> supported format)
-------------------------------------------------------------------------
The format_name is split on commas into a lowercased token set, and the stream
``codec_name`` values are used to confirm an audio stream is present where the
container is ambiguous:

* token ``wav`` present                        -> ``"wav"``
* token ``mp3`` present                        -> ``"mp3"``
* token ``webm`` present (the ``matroska,webm`` alias) and the file has an
  audio stream                                 -> ``"webm"`` (else ``None``)
* token ``m4a`` present (the ``mov,mp4,m4a,...`` alias) and the file has an
  audio stream                                 -> ``"m4a"`` (else ``None``)
* anything else / no audio stream              -> ``None``

The audio-stream check is what rejects a **video-only** file that happens to use
the shared mp4/mov/m4a or matroska/webm container: such a file exposes only a
video codec (e.g. ``h264``, ``vp9``) and no audio codec, so it maps to ``None``
(415). A genuine ``.m4a`` exposes an audio codec (e.g. ``aac``, ``alac``); a
``.webm`` exposes ``opus``/``vorbis``. Cover-art (e.g. an ``mjpeg`` thumbnail
stream) alongside a real audio stream still maps correctly because the presence
of *any* audio stream is sufficient.

Injection seam (testability)
----------------------------
``ffprobe`` is part of the ffmpeg toolchain and may be absent in some
environments (e.g. CI / local dev). The subprocess call is therefore performed
through an injectable ``runner`` callable (defaulting to :func:`_default_runner`,
a thin wrapper over ``subprocess.run`` that returns ffprobe's stdout). Tests
inject a fake runner returning captured ffprobe JSON so the endpoint path is
exercised without the binary. The pure :func:`parse_ffprobe_format` needs no
runner at all.

Failure handling
----------------
If ffprobe is unavailable, times out, exits non-zero (unreadable / corrupt
input), or emits unparseable output, :func:`detect_audio_format` returns
``None`` -- i.e. the upload is treated as an unsupported format (415). The
function never raises for these conditions, so the endpoint can map a ``None``
result directly to a 415 response.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

__all__ = [
    "detect_audio_format",
    "parse_ffprobe_format",
    "SUPPORTED_FORMATS",
]

# Canonical Supported_Audio_Format strings (mirrors Settings.supported_formats).
# Used as the mapping target; the endpoint still validates the returned value
# against the *configured* ``settings.supported_formats`` so configuration stays
# the single source of truth.
SUPPORTED_FORMATS: tuple[str, ...] = ("mp3", "wav", "m4a", "webm")

# Codecs that indicate a *video* (or image) stream rather than audio. A stream
# whose codec is not in this set is treated as audio for the audio-stream check.
# The set need not be exhaustive: it only has to recognize the common video /
# image codecs that appear inside the ambiguous mp4/mov/m4a and matroska/webm
# containers so a video-only file is rejected.
_VIDEO_CODECS: frozenset[str] = frozenset(
    {
        "h264",
        "hevc",
        "h265",
        "vp8",
        "vp9",
        "av1",
        "mpeg4",
        "mpeg2video",
        "mpeg1video",
        "mjpeg",
        "png",
        "bmp",
        "gif",
        "theora",
        "prores",
        "dvvideo",
        "wmv1",
        "wmv2",
        "wmv3",
        "vc1",
        "flv1",
        "rawvideo",
    }
)

# A runner takes the file path and returns ffprobe's raw stdout (JSON text), or
# raises a subprocess error / FileNotFoundError which the caller maps to None.
FfprobeRunner = Callable[[str], str]


def _has_audio_stream(codec_names: list[str]) -> bool:
    """Return True if any reported stream looks like an audio stream.

    A stream counts as audio when it has a non-empty ``codec_name`` that is not
    one of the known video/image codecs in :data:`_VIDEO_CODECS`. This is the
    signal that rejects a video-only file using a shared container (mp4 / webm):
    such a file exposes only video codecs and therefore has no audio stream.
    """
    return any(
        bool(name) and name.strip().lower() not in _VIDEO_CODECS
        for name in codec_names
    )


def _map_format(format_tokens: set[str], codec_names: list[str]) -> str | None:
    """Map a tokenized ffprobe ``format_name`` (+ codecs) to a supported format.

    See the module docstring for the full rule table. ``format_tokens`` is the
    lowercased set of comma-separated container aliases; ``codec_names`` is the
    list of per-stream ``codec_name`` values used to confirm an audio stream for
    the ambiguous (shared-container) cases.

    :returns: one of ``mp3`` / ``wav`` / ``m4a`` / ``webm``, or ``None`` if the
        content does not map to a supported audio format.
    """
    # Unambiguous single-purpose containers: the token alone identifies an audio
    # file (wav and mp3 are inherently audio containers).
    if "wav" in format_tokens:
        return "wav"
    if "mp3" in format_tokens:
        return "mp3"

    # Shared containers: the same format_name alias list is used by audio-only
    # and video files, so require an actual audio stream to accept them.
    if "webm" in format_tokens:
        return "webm" if _has_audio_stream(codec_names) else None
    if "m4a" in format_tokens:
        return "m4a" if _has_audio_stream(codec_names) else None

    return None


def parse_ffprobe_format(ffprobe_json: str) -> str | None:
    """Map ffprobe ``-of json`` output to a supported format, or ``None``.

    Pure function over the ffprobe payload produced by::

        ffprobe -v error -show_entries format=format_name \
            -show_entries stream=codec_name -of json <path>

    i.e. ``{"streams": [{"codec_name": ...}, ...], "format": {"format_name": ...}}``.
    The container ``format_name`` is tokenized on commas and matched against the
    supported set via :func:`_map_format`; stream ``codec_name`` values confirm
    an audio stream for the ambiguous shared-container cases.

    This is the unit-testable seam: feed it captured ffprobe JSON and assert the
    mapping without invoking the binary.

    :param ffprobe_json: the raw stdout from ffprobe.
    :returns: the canonical supported format string, or ``None`` when the
        payload is unparseable, lacks a usable ``format_name``, or maps to no
        supported audio format. Never raises for malformed input.
    """
    try:
        parsed = json.loads(ffprobe_json)
    except (json.JSONDecodeError, TypeError):
        # Garbled / non-string payload: cannot determine a format -> unsupported.
        return None

    if not isinstance(parsed, dict):
        return None

    fmt = parsed.get("format")
    format_name = ""
    if isinstance(fmt, dict):
        raw = fmt.get("format_name")
        if isinstance(raw, str):
            format_name = raw

    format_tokens = {
        token.strip().lower() for token in format_name.split(",") if token.strip()
    }

    codec_names: list[str] = []
    streams = parsed.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, dict):
                codec = stream.get("codec_name")
                if isinstance(codec, str):
                    codec_names.append(codec)

    if not format_tokens:
        return None

    return _map_format(format_tokens, codec_names)


def _default_runner(path: str, *, timeout: float = 30.0) -> str:
    """Default ``runner``: run ffprobe over ``path`` and return its stdout.

    Uses ``check=True`` so a non-zero exit (unreadable / corrupt input) raises
    :class:`subprocess.CalledProcessError`, which :func:`detect_audio_format`
    maps to ``None``. On timeout :func:`subprocess.run` kills the child and
    re-raises :class:`subprocess.TimeoutExpired`.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=format_name",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        path,
    ]
    completed = subprocess.run(  # noqa: S603 - argv built from literals + path
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return completed.stdout


def detect_audio_format(
    path: str, *, runner: FfprobeRunner = _default_runner
) -> str | None:
    """Detect the Supported_Audio_Format of the file at ``path`` by content.

    Runs ffprobe (via the injectable ``runner``) and maps its output with
    :func:`parse_ffprobe_format`. The decision is based purely on the file's
    content, never on its name/extension (Req 3.2).

    :param path: filesystem path to the (temp) uploaded file to inspect.
    :param runner: subprocess seam returning ffprobe's stdout for ``path``;
        defaults to :func:`_default_runner`. Tests inject a fake returning
        captured JSON so no real ffprobe binary is needed.
    :returns: one of ``mp3`` / ``wav`` / ``m4a`` / ``webm`` when the content maps
        to a supported format; otherwise ``None`` (unsupported / unknown /
        non-audio / unreadable). Never raises for ffprobe failures -- a missing
        binary, non-zero exit, timeout, or unparseable output all yield ``None``
        so the caller can return a 415.
    """
    try:
        output = runner(path)
    except FileNotFoundError:
        # ffprobe not installed / not on PATH: cannot inspect -> unsupported.
        return None
    except subprocess.TimeoutExpired:
        return None
    except subprocess.CalledProcessError:
        # Non-zero exit: ffprobe could not read the input (corrupt / unknown).
        return None

    return parse_ffprobe_format(output)
