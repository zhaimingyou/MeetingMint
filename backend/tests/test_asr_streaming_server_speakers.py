"""Tests for the deploy/asr-service streaming server speaker helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_streaming_server():
    root = Path(__file__).resolve().parents[2]
    script = root / "deploy" / "asr-service" / "scripts" / "streaming_server.py"

    # The deployed script imports model/runtime packages at module import time.
    # Replace the heavy model import with a tiny stub so pure helpers can be tested.
    fake_funasr = types.ModuleType("funasr")
    fake_funasr.AutoModel = object
    old_funasr = sys.modules.get("funasr")
    sys.modules["funasr"] = fake_funasr
    try:
        spec = importlib.util.spec_from_file_location("asr_streaming_server_test", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_funasr is None:
            sys.modules.pop("funasr", None)
        else:
            sys.modules["funasr"] = old_funasr


def test_speaker_label_maps_first_two_speakers_to_a_and_b() -> None:
    server = _load_streaming_server()

    assert server.speaker_label(0) == "A"
    assert server.speaker_label("0") == "A"
    assert server.speaker_label(1) == "B"
    assert server.speaker_label("spk_1") == "B"


def test_result_speaker_uses_sentence_info_when_available() -> None:
    server = _load_streaming_server()

    assert server.result_speaker({"sentence_info": [{"spk": 1}]}) == "B"
    assert server.result_speaker({"sentence_info": []}) is None
