#!/usr/bin/env python3
"""
ASR HTTP server with speaker diarization — 带说话人分离的语音识别服务.

Replaces ``funasr-server`` to add cam++ speaker diarization support.
OpenAI-compatible API on port 19100.

Pipeline: Paraformer (ASR) + FSMN (VAD) + CT-Transformer (punctuation) + cam++ (speaker).

Protocol (same as funasr-server):
    GET  /health                  -> {"status":"ok","models_loaded":[...]}
    GET  /v1/models               -> {"object":"list","data":[...]}
    POST /v1/audio/transcriptions -> multipart: file=@audio.wav, model=..., response_format=verbose_json
"""

from __future__ import annotations

import os
import time
import json
import logging
import tempfile
import contextlib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config — set BEFORE importing funasr so model cache is correct
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "MODELSCOPE_CACHE", "/opt/asr-service/models/modelscope"
)

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from funasr import AutoModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("asr-spk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HOST = os.environ.get("ASR_HOST", "0.0.0.0")
PORT = int(os.environ.get("ASR_PORT", "19100"))
DEVICE = os.environ.get("ASR_DEVICE", "cuda:0")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="ASR with Speaker Diarization", version="2.0.0")

# Loaded at startup; ``None`` until then.
_model: Any = None


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def _load_model() -> Any:
    """Load the Paraformer + cam++ pipeline once and keep in memory."""
    log.info("Loading Paraformer + cam++ speaker diarization pipeline ...")
    t0 = time.monotonic()
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="cam++",
        device=DEVICE,
        disable_update=True,
    )
    elapsed = time.monotonic() - t0
    log.info("Pipeline loaded in %.1f s", elapsed)
    return model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_segments(sentence_info: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert FunASR ``sentence_info`` to OpenAI-compatible segments."""
    segments: list[dict[str, Any]] = []
    for i, sent in enumerate(sentence_info):
        spk = sent.get("spk")
        segments.append(
            {
                "id": i,
                "seek": 0,
                "start": round(float(sent.get("start", 0)), 3),
                "end": round(float(sent.get("end", 0)), 3),
                "text": sent.get("text", ""),
                "speaker": f"spk_{spk}" if spk is not None else None,
                "tokens": [],
                "temperature": 1.0,
            }
        )
    return segments


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": DEVICE,
        "models_loaded": ["paraformer", "cam++"],
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "paraformer-spk", "object": "model"},
        ],
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default="paraformer-spk"),
    response_format: str = Form(default="verbose_json"),
):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    # Save uploaded audio to a temp file.
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        content = await file.read()
        with open(tmp_path, "wb") as fh:
            fh.write(content)

        t_start = time.monotonic()
        result = _model.generate(input=tmp_path)
        elapsed = time.monotonic() - t_start

        r = result[0]
        text: str = r.get("text", "")
        sentence_info: list[dict[str, Any]] = r.get("sentence_info", [])

        log.info(
            "Transcribed %s in %.1f s — %d chars, %d segments",
            file.filename,
            elapsed,
            len(text),
            len(sentence_info),
        )

        return {
            "task": "transcribe",
            "language": "zh",
            "duration": round(elapsed, 2),
            "text": text,
            "segments": _build_segments(sentence_info),
        }
    except Exception as exc:
        log.exception("Transcription failed for %s", file.filename)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _model
    _model = _load_model()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "server_with_spk:app",
        host=HOST,
        port=PORT,
        log_level="info",
        timeout_keep_alive=300,
        limit_concurrency=4,
        limit_max_requests=1000,
    )
