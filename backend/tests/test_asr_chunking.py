"""Example/edge unit tests for ASR chunking + duration probing (task 5.1).

Covers the pure chunking math (:func:`app.clients.chunking.compute_chunks`),
the ffprobe output-parsing seam (:func:`parse_ffprobe_duration`), and the
:class:`~app.clients.http.HttpAsrClient` chunk-planning seam (``plan_chunks``
with an injected duration prober).

The hypothesis property test for Property 10 (contiguous / non-overlapping /
bounded / full coverage across arbitrary durations and thresholds) is task 5.4;
these are the focused example and edge-case tests for task 5.1.
"""

from __future__ import annotations

import pytest

from app.clients.chunking import (
    compute_chunks,
    parse_ffprobe_duration,
    probe_duration,
)
from app.clients.http import HttpAsrClient
from app.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _assert_well_formed(chunks, duration):
    """Assert chunks are contiguous, non-overlapping, bounded, fully covering."""
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == pytest.approx(duration)
    prev_end = 0.0
    for start, end in chunks:
        assert start == pytest.approx(prev_end)  # gap-free + non-overlapping
        assert end > start  # no empty window
        prev_end = end


# --------------------------------------------------------------------------- #
# compute_chunks: duration <= threshold -> single chunk
# --------------------------------------------------------------------------- #
def test_duration_below_threshold_single_chunk():
    assert compute_chunks(100.0, 300) == [(0.0, 100.0)]


def test_duration_equal_threshold_single_chunk():
    # Chunking only subdivides when duration *exceeds* threshold (Req 5.2).
    assert compute_chunks(300.0, 300) == [(0.0, 300.0)]


def test_small_duration_single_chunk():
    assert compute_chunks(0.5, 30) == [(0.0, 0.5)]


# --------------------------------------------------------------------------- #
# compute_chunks: duration > threshold -> contiguous, non-overlapping, bounded
# --------------------------------------------------------------------------- #
def test_duration_above_threshold_with_remainder():
    chunks = compute_chunks(700.0, 300)
    assert chunks == [(0.0, 300.0), (300.0, 600.0), (600.0, 700.0)]
    _assert_well_formed(chunks, 700.0)
    for start, end in chunks:
        assert end - start <= 300  # bounded
    assert chunks[-1][1] - chunks[-1][0] == 100.0  # last is shorter remainder


def test_exact_multiple_no_trailing_empty_chunk():
    chunks = compute_chunks(900.0, 300)
    assert chunks == [(0.0, 300.0), (300.0, 600.0), (600.0, 900.0)]
    # Every chunk is a full threshold-length window; no trailing empty chunk.
    assert all(end - start == 300.0 for start, end in chunks)
    _assert_well_formed(chunks, 900.0)


def test_two_exact_chunks():
    chunks = compute_chunks(600.0, 300)
    assert chunks == [(0.0, 300.0), (300.0, 600.0)]
    _assert_well_formed(chunks, 600.0)


def test_just_over_threshold():
    chunks = compute_chunks(301.0, 300)
    assert chunks == [(0.0, 300.0), (300.0, 301.0)]
    _assert_well_formed(chunks, 301.0)


def test_minimum_threshold_many_chunks():
    duration = 95.0
    chunks = compute_chunks(duration, 30)
    assert chunks == [(0.0, 30.0), (30.0, 60.0), (60.0, 90.0), (90.0, 95.0)]
    _assert_well_formed(chunks, duration)
    for start, end in chunks:
        assert end - start <= 30


# --------------------------------------------------------------------------- #
# compute_chunks: edge cases
# --------------------------------------------------------------------------- #
def test_zero_duration_returns_empty():
    # Documented decision: nothing to transcribe -> no windows.
    assert compute_chunks(0.0, 300) == []


def test_negative_duration_returns_empty():
    assert compute_chunks(-5.0, 300) == []


def test_non_positive_threshold_raises():
    with pytest.raises(ValueError):
        compute_chunks(100.0, 0)
    with pytest.raises(ValueError):
        compute_chunks(100.0, -10)


# --------------------------------------------------------------------------- #
# parse_ffprobe_duration: the testable seam for probe_duration
# --------------------------------------------------------------------------- #
def test_parse_ffprobe_duration_typical():
    payload = '{"format": {"duration": "123.456"}}'
    assert parse_ffprobe_duration(payload) == pytest.approx(123.456)


def test_parse_ffprobe_duration_integerish():
    payload = '{"format": {"duration": "600"}}'
    assert parse_ffprobe_duration(payload) == 600.0


def test_parse_ffprobe_duration_invalid_json():
    with pytest.raises(ValueError):
        parse_ffprobe_duration("not json")


def test_parse_ffprobe_duration_missing_field():
    with pytest.raises(ValueError):
        parse_ffprobe_duration('{"format": {}}')


def test_parse_ffprobe_duration_non_numeric():
    with pytest.raises(ValueError):
        parse_ffprobe_duration('{"format": {"duration": "N/A"}}')


def test_probe_duration_missing_binary(monkeypatch):
    # When ffprobe is unavailable the probe surfaces a ValueError, not a raw
    # FileNotFoundError, so callers handle a single failure type.
    import subprocess

    def _raise(*args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(ValueError):
        probe_duration("/tmp/whatever.wav")


# --------------------------------------------------------------------------- #
# HttpAsrClient.plan_chunks: chunking via an injected (fake) duration prober
# --------------------------------------------------------------------------- #
def _settings(chunk_length_seconds: int = 300) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="bootstrap-pass",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="mock",  # avoids requiring an endpoint URL at construction
        asr_chunk_length_seconds=chunk_length_seconds,
    )  # type: ignore[call-arg]


def test_plan_chunks_uses_injected_prober_and_threshold():
    client = HttpAsrClient(_settings(chunk_length_seconds=300), duration_prober=lambda _p: 700.0)
    assert client.plan_chunks("/data/audio/x.wav") == [
        (0.0, 300.0),
        (300.0, 600.0),
        (600.0, 700.0),
    ]


def test_plan_chunks_single_window_when_short():
    client = HttpAsrClient(_settings(chunk_length_seconds=300), duration_prober=lambda _p: 42.0)
    assert client.plan_chunks("/data/audio/x.wav") == [(0.0, 42.0)]


def test_plan_chunks_zero_duration_empty():
    client = HttpAsrClient(_settings(), duration_prober=lambda _p: 0.0)
    assert client.plan_chunks("/data/audio/x.wav") == []


def test_plan_chunks_respects_configured_threshold():
    client = HttpAsrClient(_settings(chunk_length_seconds=30), duration_prober=lambda _p: 95.0)
    assert client.plan_chunks("/data/audio/x.wav") == [
        (0.0, 30.0),
        (30.0, 60.0),
        (60.0, 90.0),
        (90.0, 95.0),
    ]
