"""LOCAL DEV launcher -- NOT for production.

Runs the Backend_API on http://127.0.0.1:8000 for a zero-infrastructure local
demo so the frontend can be clicked through end to end:

- mock ASR/LLM clients (no real models),
- the processing pipeline runs INLINE on upload (no Redis / Celery worker),
- the ffprobe (format detection) and ffmpeg (preprocessing) seams are stubbed so
  it works on a machine without ffmpeg installed.

Production is different and unchanged: `python -m app.entrypoint` (migrations +
bootstrap) then uvicorn for the API, a real Celery worker, and Redis -- see
docker-compose.yml. This file is purely a convenience for "看效果" and can be
deleted at any time. Local state lives under backend/.localrun/ (gitignored).
"""

from __future__ import annotations

import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUN = os.path.join(_HERE, ".localrun")

# Local, model-less configuration. setdefault so real env vars still win.
os.environ.setdefault("BOOTSTRAP_ADMIN_USERNAME", "admin")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "admin12345")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")  # unused (inline)
os.environ.setdefault(
    "DATABASE_URL", f"sqlite:///{os.path.join(_RUN, 'db', 'app.db')}"
)
os.environ.setdefault("AUDIO_STORAGE_DIR", os.path.join(_RUN, "audio"))
os.environ.setdefault("ASR_IMPL", "mock")
os.environ.setdefault("LLM_IMPL", "mock")


def _patch_for_local_dev() -> None:
    """Stub the ffprobe/ffmpeg/broker seams so the flow runs with no infra."""
    import app.api.meetings as meetings_mod
    import app.worker.tasks as tasks_mod
    from app.worker.tasks import run_pipeline

    # No ffprobe: accept the upload and label it a supported format. (The mock
    # pipeline never decodes the bytes, so the label is cosmetic here.)
    meetings_mod.detect_format = lambda _path: "webm"

    # No ffmpeg: "preprocess" by copying the source to the standardized path.
    def _fake_preprocess(source_path: str, output_path: str, timeout_seconds=None) -> str:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copyfile(source_path, output_path)
        return output_path

    tasks_mod.preprocess_meeting_audio = _fake_preprocess

    # No Redis/worker: run the pipeline inline (synchronously) when the upload
    # endpoint "enqueues", so a meeting reaches `completed` before the response.
    meetings_mod._enqueue_processing_job = lambda meeting_id: run_pipeline(meeting_id)


def main() -> None:
    from app.entrypoint import main as entry_main

    entry_main([])  # validate config -> ensure dirs -> migrate -> seed admin
    _patch_for_local_dev()

    import uvicorn

    print("\n  Local dev API on http://127.0.0.1:8000  (admin / admin12345)\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
