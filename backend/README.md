# Meeting Minutes Service — Backend

Python 3.12 + FastAPI backend for the Meeting Minutes Service. Turns meeting
audio into structured, editable, exportable Chinese-language meeting minutes via
an asynchronous pipeline (preprocess → transcribe → generate).

## Package layout

```
app/            Application package
  api/          HTTP API routers and dependencies
  clients/      External ASR/LLM clients and mock implementations
  worker/       Celery worker and async job pipeline
  main.py       FastAPI application entrypoint
tests/          Test suite (pytest + hypothesis)
```

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The ASR and LLM models are external HTTP services provisioned later. The system
is fully developable and testable without them by selecting mock client
implementations through configuration (added in later tasks).

## Running the full stack (Docker Compose)

The whole system (Web_Frontend, Backend_API, Job_Processor/worker, Redis, Nginx)
runs from the repo-root `docker-compose.yml`. Production runs on **`192.0.2.10`**
(`linux/amd64`, set `DOCKER_PLATFORM=linux/amd64` in `.env`). The repo image
defaults target `linux/arm64` (DGX Spark dev); ASR/LLM services run on
`192.0.2.20`.

```bash
cp .env.example .env        # fill in BOOTSTRAP_ADMIN_USERNAME / _PASSWORD, etc.
# On 192.0.2.10: DOCKER_PLATFORM=linux/amd64
docker compose build
docker compose up -d
# Then open https://192.0.2.10/
```

Nginx publishes port 80 — open `http://<host>/`. On startup the API container
applies database migrations and seeds the bootstrap admin
(`python -m app.entrypoint`), and the worker runs the Celery pipeline. The
SQLite database file and audio storage live on a shared `appdata` volume mounted
at `/data` in both the API and the worker.

## Switching to real ASR/LLM models

The service runs **model-less by default** using deterministic mock clients
(`ASR_IMPL=mock`, `LLM_IMPL=mock`), so the entire flow — upload, preprocess,
transcribe, generate, view/edit/export — works with no models present. To
connect the real models (no code change or image rebuild required, only
configuration + a restart), edit `.env`:

```bash
# ASR — local SenseVoice / FunASR on the DGX Spark (OpenAI-compatible).
# The client normalizes the base URL to {ASR_ENDPOINT_URL}/v1/audio/transcriptions
# and slices each chunk with ffmpeg before upload.
ASR_IMPL=http
ASR_ENDPOINT_URL=http://192.0.2.20:19100
ASR_MODEL_NAME=sensevoice

# LLM — DeepSeek (cloud, OpenAI-compatible). The client normalizes the base URL
# to {LLM_ENDPOINT_URL}/chat/completions and sends "Authorization: Bearer
# <LLM_API_KEY>". A local unauthenticated Ollama/vLLM works too — just point the
# URL at it and omit LLM_API_KEY.
LLM_IMPL=http
LLM_ENDPOINT_URL=https://api.deepseek.com
LLM_MODEL_NAME=deepseek-v4-flash
LLM_API_KEY=<your-deepseek-key>   # secrets live ONLY in .env (gitignored)
```

then `docker compose up -d` to restart. Confirm the endpoints are reachable and
the model name is valid with the bundled self-check (probes ASR `/health` and
the LLM `/models` list using your config; never prints the key):

```bash
docker compose exec api python -m app.diagnostics
```

then `docker compose up -d` to restart. Startup validation (`app.startup`) fails
fast and logs every offending key if an HTTP-mode endpoint URL or the model name
is missing or not a well-formed absolute URL (Req 10.4 / 10.5). Revert to the
mock pipeline anytime by setting `ASR_IMPL` / `LLM_IMPL` back to `mock`.

> **Privacy note:** DeepSeek is a hosted cloud service, so meeting transcripts
> are sent off the internal network to DeepSeek's servers, and the DGX Spark
> needs outbound HTTPS to `api.deepseek.com`. For a fully on-prem deployment,
> point `LLM_ENDPOINT_URL` at a locally-served OpenAI-compatible model instead.
