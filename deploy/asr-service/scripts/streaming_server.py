#!/usr/bin/env python3
"""
Real-time ASR streaming server — 实时边说边识别

WebSocket endpoint (port 19101). Receives PCM int16 16kHz mono audio frames.
Periodically transcribes during speech (partial) and after silence (final).
When FunASR returns speaker diarization data, speaker ids are mapped to A/B.

Protocol:
  Server -> Client (JSON):
    {"type": "status",  "message": "listening"}
    {"type": "partial", "text": "我们", "timestamp": 1.5, "speaker": "A"}    # 边说边出
    {"type": "final",   "text": "我们来讨论一下计划", "duration": 3.2, "speaker": "B"}
    {"type": "error",   "message": "..."}
  Client -> Server:
    Binary audio frames (PCM int16, 16kHz, mono, 100ms each)
    Text: {"type": "reset"}
"""

import asyncio
import json
import re
import struct
import time
import traceback
import os
import sys
from typing import Any

import numpy as np
import websockets
from websockets.asyncio.server import serve

os.environ.setdefault("MODELSCOPE_CACHE", "/opt/asr-service/models/modelscope")

from funasr import AutoModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WS_PORT = 19101
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MIN_SPEECH_SEC = 0.3          # 低于此长度忽略
SILENCE_TIMEOUT_SEC = 0.5     # 静音多久触发断句
PARTIAL_INTERVAL_SEC = 0.4    # 说话期间每隔多久做一次中间识别
ENERGY_THRESHOLD = 200        # 能量阈值（调低以适应 Mac 麦克风）


def speaker_label(spk: object) -> str | None:
    """Map FunASR speaker ids (0/1/spk_0/spk_1) to UI labels A/B."""
    if spk is None:
        return None
    text = str(spk).strip()
    if not text:
        return None
    match = re.search(r"(\d+)$", text)
    if not match:
        return text
    idx = int(match.group(1))
    if 0 <= idx < 26:
        return chr(ord("A") + idx)
    return f"Speaker {idx + 1}"


def result_speaker(result: dict[str, Any]) -> str | None:
    """Pick the dominant speaker from a FunASR generate result."""
    sentence_info = result.get("sentence_info")
    if not isinstance(sentence_info, list) or not sentence_info:
        return None

    weights: dict[object, float] = {}
    for sent in sentence_info:
        if not isinstance(sent, dict):
            continue
        spk = sent.get("spk")
        if spk is None:
            continue
        try:
            weight = max(0.0, float(sent.get("end", 0)) - float(sent.get("start", 0)))
        except (TypeError, ValueError):
            weight = 0.0
        weights[spk] = weights.get(spk, 0.0) + (weight or 1.0)
    if not weights:
        return None
    return speaker_label(max(weights, key=weights.get))


class StreamingASR:
    """ASR model wrapper — transcribes PCM audio chunks with speaker labels."""

    def __init__(self):
        print("[server] Loading Paraformer + cam++ for streaming...")
        self.model = AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            spk_model="cam++",
            disable_pbar=True,
            device="cuda:0",
            disable_update=True,
        )
        print("[server] Model loaded.")

    def transcribe(self, pcm_bytes: bytes) -> dict[str, str | None]:
        """Transcribe PCM int16 bytes -> clean text plus optional speaker."""
        min_bytes = int(SAMPLE_RATE * BYTES_PER_SAMPLE * MIN_SPEECH_SEC)
        if len(pcm_bytes) < min_bytes:
            return {"text": "", "speaker": None}

        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            result = self.model.generate(
                input=audio,
                language="zh",
                use_itn=True,
                batch_size_s=60,
            )
        except Exception:
            traceback.print_exc()
            return {"text": "", "speaker": None}

        if result and len(result) > 0:
            item = result[0]
            text = item.get("text", "")
            # Paraformer includes punctuation natively; SenseVoice <|tag|>
            # artifacts won't appear, but strip them just in case.
            text = re.sub(r"<\|[^|]+\|>", "", text).strip()
            return {"text": text, "speaker": result_speaker(item)}
        return {"text": "", "speaker": None}


class SpeechTracker:
    """
    Tracks speech state with energy-based VAD.
    Supports both interim (partial) and final transcription.
    """

    def __init__(self):
        self.buffer: list[bytes] = []
        self.speech_started = False
        self.silence_start: float | None = None
        self.last_partial_time = 0.0
        self.total_duration = 0.0

    def has_energy(self, pcm: bytes) -> bool:
        if len(pcm) < 2:
            return False
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
        rms = np.sqrt(np.mean(np.array(samples, dtype=np.float64) ** 2))
        return rms > ENERGY_THRESHOLD

    def chunk_duration(self) -> float:
        """Duration of a single chunk in seconds."""
        return 1.0 / SAMPLE_RATE / BYTES_PER_SAMPLE  # ~0.006ms per sample...
        # Actually each chunk is 100ms (defined by client CHUNK_SIZE)
        # Let's compute from byte length instead

    def add_chunk(self, pcm: bytes, now: float) -> tuple[str | None, str]:
        """
        Process incoming audio chunk.

        Returns:
          (action, extra_info)
          action="partial"  — do an interim transcription
          action="finalize" — silence timeout; do final transcription
          action="flush"    — remaining speech when disconnecting
          action=None       — keep accumulating
        """
        chunk_sec = len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        energy = self.has_energy(pcm)

        if energy:
            if not self.speech_started:
                self.speech_started = True
                self.last_partial_time = now
                self.buffer = [pcm]
                self.total_duration = chunk_sec
            else:
                self.buffer.append(pcm)
                self.total_duration += chunk_sec
            self.silence_start = None

            # Check if it's time for a partial transcription
            if now - self.last_partial_time >= PARTIAL_INTERVAL_SEC and self.total_duration >= 0.5:
                self.last_partial_time = now
                return "partial", ""
            return None, ""

        else:  # silence / low energy
            if not self.speech_started:
                return None, ""

            self.buffer.append(pcm)
            self.total_duration += chunk_sec

            if self.silence_start is None:
                self.silence_start = now

            silence_dur = now - self.silence_start
            if silence_dur >= SILENCE_TIMEOUT_SEC and self.total_duration >= MIN_SPEECH_SEC:
                return "finalize", ""
            return None, ""

    def get_speech_bytes(self) -> bytes:
        return b"".join(self.buffer)

    def get_duration(self) -> float:
        return self.total_duration

    def reset(self):
        self.buffer = []
        self.speech_started = False
        self.silence_start = None
        self.last_partial_time = 0.0
        self.total_duration = 0.0

    def flush(self) -> str | None:
        """Finalize remaining audio, return action."""
        if self.speech_started and self.total_duration >= MIN_SPEECH_SEC:
            return "flush"
        self.reset()
        return None


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
async def handle_client(websocket, asr: StreamingASR):
    tracker = SpeechTracker()
    last_partial_text = ""  # dedup partial results

    print(f"[server] Connected: {websocket.remote_address}")
    await websocket.send(json.dumps({"type": "status", "message": "listening"}))

    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    cmd = json.loads(message)
                    if cmd.get("type") == "reset":
                        tracker.reset()
                        last_partial_text = ""
                        await websocket.send(json.dumps({
                            "type": "status", "message": "reset"
                        }))
                except json.JSONDecodeError:
                    pass
                continue

            now = time.time()
            action, _ = tracker.add_chunk(message, now)

            if action == "partial":
                audio = tracker.get_speech_bytes()
                res = asr.transcribe(audio)
                text = res["text"] or ""
                if text and text != last_partial_text:
                    last_partial_text = text
                    msg = {
                        "type": "partial",
                        "text": text,
                        "timestamp": tracker.get_duration(),
                    }
                    if res["speaker"]:
                        msg["speaker"] = res["speaker"]
                    await websocket.send(json.dumps(msg, ensure_ascii=False))

            elif action in ("finalize", "flush"):
                audio = tracker.get_speech_bytes()
                dur = tracker.get_duration()
                res = asr.transcribe(audio)
                text = res["text"] or ""
                tracker.reset()
                last_partial_text = ""
                if text:
                    msg = {
                        "type": "final",
                        "text": text,
                        "duration": round(dur, 1),
                    }
                    if res["speaker"]:
                        msg["speaker"] = res["speaker"]
                    await websocket.send(json.dumps(msg, ensure_ascii=False))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        action = tracker.flush()
        if action == "flush":
            audio = tracker.get_speech_bytes()
            res = asr.transcribe(audio)
            text = res["text"] or ""
            if text:
                try:
                    msg = {
                        "type": "final",
                        "text": text,
                    }
                    if res["speaker"]:
                        msg["speaker"] = res["speaker"]
                    await websocket.send(json.dumps(msg, ensure_ascii=False))
                except Exception:
                    pass
        print(f"[server] Disconnected: {websocket.remote_address}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    asr = StreamingASR()
    print(f"[server] Streaming WebSocket on ws://0.0.0.0:{WS_PORT}")
    async with serve(
        lambda ws: handle_client(ws, asr),
        host="0.0.0.0",
        port=WS_PORT,
        max_size=10 * 1024 * 1024,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
