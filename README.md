# MeetingMint 🍃

**Fresh minutes from every meeting.**

English | [简体中文](README.zh-CN.md)

Upload a meeting recording and get back a structured, editable, exportable
minutes document. MeetingMint is a self-hosted meeting-minutes service with
both batch and real-time transcription, pluggable ASR / LLM endpoints, and
all data staying inside your own network.

## Features

- **📤 Batch pipeline** — upload up to 500 MB of meeting audio; an async
  Celery pipeline runs preprocess → transcribe → generate and produces
  structured, editable, exportable meeting minutes.
- **🔴 Live meetings** — real-time transcription over WebSocket streaming
  ASR, with live speaker labels and automatic recovery after a mid-meeting
  connection drop.
- **🔌 Pluggable models** — ASR and LLM are external OpenAI-compatible HTTP
  services (SenseVoice / FunASR, DeepSeek, vLLM, Ollama, …). Deterministic
  `mock` implementations let you run and test the whole system with no
  models deployed at all.
- **👥 Accounts & admin console** — self-service registration, per-user
  meeting workspaces, and an admin console for user management, runtime
  configuration, job monitoring and an audit log.
- **🐳 Self-hosted** — one `docker compose up` with Nginx (TLS) in front;
  audio, transcripts and logs never leave your network.

## Architecture

```
frontend (Vite + TypeScript)
   │
nginx (TLS, port 80/443)
   │
api (FastAPI) ── redis ── worker (Celery) ──► ASR service (OpenAI-compatible HTTP)
   │                                            LLM service (OpenAI-compatible HTTP)
postgres + audio/file storage
```

The ASR / LLM integrations are client abstractions behind configuration
(`ASR_IMPL` / `LLM_IMPL` = `mock` | `http`), so the backend is fully
developable and testable without any model service.

## Quick start

```bash
cp .env.example .env      # defaults run everything in mock mode
docker compose up -d --build
```

Open <http://localhost/>, sign in with the bootstrap admin account from
`.env` (`BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` — change the
default before any real deployment), and create your first meeting.

With `ASR_IMPL=mock` / `LLM_IMPL=mock` the pipeline runs end-to-end with
deterministic stand-ins — useful for trying the UI and the API without any
models.

## Configuration

Copy `.env.example` to `.env` and fill in what you need. Key settings:

| Variable | Purpose |
|---|---|
| `ASR_IMPL` / `LLM_IMPL` | `mock` (no models needed) or `http` (real endpoints, validated at startup) |
| `ASR_ENDPOINT_URL` | OpenAI-compatible transcription endpoint, e.g. a SenseVoice/FunASR server |
| `LLM_ENDPOINT_URL` / `LLM_MODEL_NAME` / `LLM_API_KEY` | Any OpenAI-compatible chat endpoint (DeepSeek, local vLLM / Ollama, …) |
| `STREAMING_ASR_WS_URL` | WebSocket streaming ASR endpoint for live meetings |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | Initial admin account, hashed at startup |
| `MAX_UPLOAD_BYTES` | Upload size cap (default 500 MB) |

See `.env.example` for the full annotated list.

## Development

**Backend** (Python 3.12, FastAPI, Celery, SQLAlchemy/Alembic):

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

**Frontend** (Vite + TypeScript + React):

```bash
cd frontend
npm ci
npm run dev        # or: npm run build / typecheck / test
```

## Documentation

- [`DEPLOY.md`](DEPLOY.md) — production deployment / update manual (Docker Compose + Nginx)
- [`backend/README.md`](backend/README.md) — backend layout and development guide
- [`deploy/asr-service/ASR-API.md`](deploy/asr-service/ASR-API.md) — ASR / streaming ASR service API

## License

[MIT](LICENSE)
