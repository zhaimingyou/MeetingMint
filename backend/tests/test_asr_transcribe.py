"""Example/edge unit tests for HttpAsrClient.transcribe orchestration (task 5.2).

These focus on the orchestration that task 5.2 owns:

* happy-path multi-chunk assembly with **timestamp offsetting** and **global
  ascending-start ordering** (Req 5.3 / Property 11),
* **speaker mapping** -- a label is carried only when the ASR response provides
  one, else ``None`` (Req 5.4 / Property 12),
* the **bounded retry** loop -- a chunk that fails ``N < max_retries`` times then
  succeeds is retried exactly the expected number of times; a chunk that always
  fails raises :class:`AsrError` after ``max_retries + 1`` attempts (terminal
  behavior is verified thoroughly in task 5.3),
* **zero-duration** audio assembles an empty transcript with no chunk calls.

The chunk-transcriber and duration-prober seams are injected, so no real
ASR_Service, network, or ffmpeg is used. The hypothesis property tests for
Properties 11/12 are task 5.4; the terminal-failure Property 13 is task 5.3.
"""

from __future__ import annotations

import httpx
import pytest

from app.clients.base import AsrError
from app.clients.http import HttpAsrClient
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings(
    *, chunk_length_seconds: int = 300, max_retries: int = 2
) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="mock",  # avoids requiring an endpoint URL at construction
        asr_chunk_length_seconds=chunk_length_seconds,
        asr_max_retries=max_retries,
    )  # type: ignore[call-arg]


def _client(
    *,
    duration: float,
    chunk_transcriber,
    chunk_length_seconds: int = 300,
    max_retries: int = 2,
) -> HttpAsrClient:
    """Build an HttpAsrClient with both seams injected (no network/ffmpeg)."""
    return HttpAsrClient(
        _settings(chunk_length_seconds=chunk_length_seconds, max_retries=max_retries),
        duration_prober=lambda _path: duration,
        chunk_transcriber=chunk_transcriber,
    )


# --------------------------------------------------------------------------- #
# Happy path: offsetting + assembly ordering
# --------------------------------------------------------------------------- #
def test_single_chunk_no_offset():
    # Duration <= threshold -> one chunk [0, duration]; no offset applied.
    def transcriber(_path, start, end):
        return [
            {"start": 0.0, "end": 1.0, "text": "你好", "speaker": None},
            {"start": 1.0, "end": 2.5, "text": "大家好", "speaker": None},
        ]

    client = _client(duration=42.0, chunk_transcriber=transcriber)
    segments = client.transcribe("/data/audio/x.wav")

    assert segments == [
        {"start": 0.0, "end": 1.0, "text": "你好", "speaker": None},
        {"start": 1.0, "end": 2.5, "text": "大家好", "speaker": None},
    ]


def test_multi_chunk_offsets_by_chunk_start_and_orders():
    # duration 700, threshold 300 -> chunks [0,300],[300,600],[600,700].
    # Each chunk returns chunk-relative timestamps that must be offset.
    def transcriber(_path, start, end):
        # one segment per chunk, chunk-relative starting at 10s into the chunk.
        return [{"start": 10.0, "end": 20.0, "text": f"chunk@{start}", "speaker": None}]

    client = _client(duration=700.0, chunk_transcriber=transcriber)
    segments = client.transcribe("/data/audio/x.wav")

    # Offsets: 10->10, 10+300->310, 10+600->610; ordered ascending by start.
    assert [s["start"] for s in segments] == [10.0, 310.0, 610.0]
    assert [s["end"] for s in segments] == [20.0, 320.0, 620.0]
    assert [s["text"] for s in segments] == ["chunk@0.0", "chunk@300.0", "chunk@600.0"]


def test_assembled_segments_globally_ordered_by_start():
    # A chunk returns segments out of order; assembly sorts the global list.
    def transcriber(_path, start, end):
        if start == 0.0:
            return [
                {"start": 5.0, "end": 6.0, "text": "b", "speaker": None},
                {"start": 1.0, "end": 2.0, "text": "a", "speaker": None},
            ]
        return [{"start": 0.0, "end": 1.0, "text": "c", "speaker": None}]

    # duration 400, threshold 300 -> chunks [0,300],[300,400].
    client = _client(duration=400.0, chunk_transcriber=transcriber)
    segments = client.transcribe("/data/audio/x.wav")

    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)
    # chunk2 segment offset by 300 -> start 300.0; chunk1 -> 1.0 and 5.0.
    assert starts == [1.0, 5.0, 300.0]
    assert [s["text"] for s in segments] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Speaker mapping (Req 5.4 / Property 12)
# --------------------------------------------------------------------------- #
def test_speaker_label_mapped_when_present_else_none():
    def transcriber(_path, start, end):
        return [
            {"start": 0.0, "end": 1.0, "text": "with speaker", "speaker": "发言人A"},
            {"start": 1.0, "end": 2.0, "text": "no speaker key"},  # absent
            {"start": 2.0, "end": 3.0, "text": "explicit none", "speaker": None},
        ]

    client = _client(duration=10.0, chunk_transcriber=transcriber)
    segments = client.transcribe("/data/audio/x.wav")

    assert segments[0]["speaker"] == "发言人A"
    assert segments[1]["speaker"] is None  # missing key -> None
    assert segments[2]["speaker"] is None  # explicit None -> None


# --------------------------------------------------------------------------- #
# Bounded retry loop (Req 5.5)
# --------------------------------------------------------------------------- #
def test_retries_then_succeeds_counts_attempts():
    calls = {"n": 0}

    def transcriber(_path, start, end):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, succeed on the 3rd attempt
            raise httpx.ConnectError("boom")
        return [{"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}]

    # max_retries=2 -> up to 3 attempts; single chunk (short duration).
    client = _client(duration=5.0, chunk_transcriber=transcriber, max_retries=2)
    segments = client.transcribe("/data/audio/x.wav")

    assert calls["n"] == 3  # 2 failures + 1 success
    assert segments == [{"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}]


def test_always_failing_chunk_raises_asr_error_after_attempts():
    calls = {"n": 0}

    def transcriber(_path, start, end):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    client = _client(duration=5.0, chunk_transcriber=transcriber, max_retries=2)
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")

    # max_retries + 1 attempts on the single (index 0) chunk.
    assert calls["n"] == 3
    assert excinfo.value.chunk_index == 0


def test_retry_bound_zero_means_single_attempt():
    calls = {"n": 0}

    def transcriber(_path, start, end):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = _client(duration=5.0, chunk_transcriber=transcriber, max_retries=0)
    with pytest.raises(AsrError):
        client.transcribe("/data/audio/x.wav")
    assert calls["n"] == 1  # 0 retries -> exactly one attempt


def test_failing_second_chunk_reports_its_index():
    def transcriber(_path, start, end):
        if start == 0.0:
            return [{"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}]
        raise httpx.ConnectError("boom on chunk 2")

    # duration 400, threshold 300 -> chunk 0 [0,300] ok, chunk 1 [300,400] fails.
    client = _client(duration=400.0, chunk_transcriber=transcriber, max_retries=1)
    with pytest.raises(AsrError) as excinfo:
        client.transcribe("/data/audio/x.wav")
    assert excinfo.value.chunk_index == 1


# --------------------------------------------------------------------------- #
# Zero-duration audio -> empty transcript, no chunk calls
# --------------------------------------------------------------------------- #
def test_zero_duration_returns_empty_without_calling_transcriber():
    calls = {"n": 0}

    def transcriber(_path, start, end):  # pragma: no cover - must not be called
        calls["n"] += 1
        return []

    client = _client(duration=0.0, chunk_transcriber=transcriber)
    assert client.transcribe("/data/audio/x.wav") == []
    assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# _transcribe_chunk default seam: httpx via MockTransport (no real server)
# --------------------------------------------------------------------------- #
def test_default_transcribe_chunk_uses_httpx_and_parses_response(tmp_path, monkeypatch):
    # Write a tiny fake audio file so open() succeeds.
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content"] = request.content
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "你好", "speaker": "发言人A"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)

    # Patch httpx.Client used inside _transcribe_chunk to use the mock transport.
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)

    settings_http = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="http",
        asr_endpoint_url="http://asr.internal:19100",
        asr_model_name="sensevoice",
        asr_chunk_length_seconds=300,
        asr_max_retries=1,
    )  # type: ignore[call-arg]
    # Inject a no-op slicer (return the source path) so no real ffmpeg runs;
    # the default _transcribe_chunk then uploads that file as the chunk.
    client = HttpAsrClient(
        settings_http,
        duration_prober=lambda _p: 5.0,
        audio_slicer=lambda path, _s, _e: path,
    )

    segments = client.transcribe(str(audio))

    assert captured["method"] == "POST"
    # The base endpoint is normalized to the OpenAI-compatible transcriptions URL.
    assert captured["url"] == "http://asr.internal:19100/v1/audio/transcriptions"
    # The multipart body carries the file part + model + verbose_json format.
    body = captured["content"]
    assert b'name="file"' in body
    assert b"sensevoice" in body
    assert b"verbose_json" in body
    assert segments == [
        {"start": 0.0, "end": 1.0, "text": "你好", "speaker": "发言人A"},
    ]


def test_default_transcribe_chunk_retries_on_error_status(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFfake")

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=[{"start": 0.0, "end": 1.0, "text": "ok"}])

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)

    settings_http = Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="http",
        asr_endpoint_url="http://asr.internal:19100",
        asr_model_name="sensevoice",
        asr_chunk_length_seconds=300,
        asr_max_retries=2,
    )  # type: ignore[call-arg]
    client = HttpAsrClient(
        settings_http,
        duration_prober=lambda _p: 5.0,
        audio_slicer=lambda path, _s, _e: path,
    )

    segments = client.transcribe(str(audio))
    assert attempts["n"] == 2  # one 503 retried, then success
    assert segments == [{"start": 0.0, "end": 1.0, "text": "ok", "speaker": None}]
