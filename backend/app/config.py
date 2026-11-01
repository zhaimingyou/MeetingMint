"""Application configuration (Configuration_Store).

Defines the :class:`Settings` model loaded from environment variables and an
optional ``.env`` file via ``pydantic-settings`` (v2). The field set and
defaults mirror the design's "Configuration_Store (Pydantic Settings)" section.

Scope note: this module only declares the settings schema, basic type/range
validation (via ``Field`` constraints), and a cached accessor. The fail-fast
startup validation -- the HTTP-vs-mock conditional requirements for endpoint
URLs / model name, logging every offending key, and aborting the process with a
non-zero exit -- is intentionally NOT implemented here; it belongs to a later
task. Cross-field validation is therefore deliberately absent.

Environment variables map to field names in UPPER_SNAKE_CASE by default
(``pydantic-settings`` is case-insensitive). For example, the field
``bootstrap_admin_username`` is read from ``BOOTSTRAP_ADMIN_USERNAME``. No env
prefix is used, keeping variable names short and explicit.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Required values (no defaults) must be supplied via the environment or the
    ``.env`` file: ``bootstrap_admin_username``, ``bootstrap_admin_password``,
    and ``redis_url``. All other fields fall back to the documented defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Auth -------------------------------------------------------------
    session_ttl_minutes: int = 60
    max_credential_length: int = 256
    # Bootstrap: the single initial admin account is seeded at startup from
    # these values (password is hashed before storage; never persisted in
    # plaintext, never committed -- supplied via environment/secret at deploy).
    bootstrap_admin_username: str
    bootstrap_admin_password: str

    # --- Uploads ----------------------------------------------------------
    max_upload_bytes: int = 500 * 1024 * 1024  # 500 MB default (Req 3.3)
    supported_formats: list[str] = ["mp3", "wav", "m4a", "webm"]

    # --- Export -----------------------------------------------------------
    # MVP export format (Req 9.3); ".docx" is a future option.
    export_format: Literal["markdown"] = "markdown"

    # --- Preprocessing ----------------------------------------------------
    preprocess_timeout_seconds: int = Field(default=600, ge=1, le=600)  # Req 4.3

    # --- ASR client -------------------------------------------------------
    asr_impl: Literal["http", "mock"] = "http"  # Req 11.1
    asr_endpoint_url: AnyHttpUrl | None = None  # required when asr_impl == "http" (task 1.3)
    # Model id sent to the OpenAI-compatible ASR endpoint
    # (POST {base}/v1/audio/transcriptions). The DGX Spark FunASR service loads
    # "sensevoice" by default (see deploy/asr-service/ASR-API.md); override via
    # ASR_MODEL_NAME to select another loaded model (e.g. paraformer).
    asr_model_name: str = "sensevoice"
    asr_request_timeout_seconds: int = Field(default=120, ge=1, le=600)  # Req 5.1
    asr_chunk_length_seconds: int = Field(default=300, ge=30, le=600)  # Req 5.2
    asr_max_retries: int = Field(default=2, ge=0, le=5)  # Req 5.5
    # When True (default), the pipeline runs an LLM-based phonetic correction
    # pass on the full transcript after ASR, before generating minutes. Set to
    # False to skip correction (faster, but may contain homophone errors).
    asr_correction_enabled: bool = True

    # --- LLM client -------------------------------------------------------
    llm_impl: Literal["http", "mock"] = "http"  # Req 11.2
    llm_endpoint_url: AnyHttpUrl | None = None  # required when llm_impl == "http" (task 1.3)
    llm_model_name: str | None = None  # required when llm_impl == "http" (task 1.3)
    # Optional bearer token for the LLM endpoint. Required by hosted providers
    # (e.g. DeepSeek: Authorization: Bearer <key>); leave unset for a local,
    # unauthenticated LLM (Ollama / vLLM). Supplied via env/secret (LLM_API_KEY)
    # and NEVER committed.
    llm_api_key: str | None = None
    llm_request_timeout_seconds: int = Field(default=120, ge=1, le=600)  # Req 6.1
    # Chain-of-thought control for Qwen3-style chat templates (the local
    # llama.cpp llama-server). Only effective when the endpoint honors
    # ``chat_template_kwargs.enable_thinking`` (Qwen3 family); other providers
    # (e.g. DeepSeek) ignore the extra field, so any value is always safe.
    #   "auto" (default): thinking ON for minutes extraction (quality), OFF for
    #     post-ASR correction (speed) -- best balance on a thinking model
    #     (~5x faster correction, identical output).
    #   "on":  thinking enabled for every LLM call (max quality, slowest).
    #   "off": thinking disabled for every LLM call (fastest).
    llm_thinking: Literal["auto", "on", "off"] = "auto"

    # --- Streaming ASR client (live path; realtime-meeting-transcription) -
    # Selects the streaming transcription implementation, mirroring the batch
    # ``asr_impl`` convention. Defaults to ``mock`` so the whole live experience
    # runs model-less (Req 4.1, 4.2 of realtime-meeting-transcription).
    streaming_asr_impl: Literal["http", "mock", "websocket"] = "mock"
    # Required only when ``streaming_asr_impl == "http"`` (fail-fast in
    # app.startup); ignored in mock mode (Req 4.4, 4.5). Well-formedness is
    # enforced by ``AnyHttpUrl`` at construction, exactly like ``asr_endpoint_url``.
    streaming_asr_endpoint_url: AnyHttpUrl | None = None
    # Required only when ``streaming_asr_impl == "websocket"``. The WebSocket
    # URL of the FunASR streaming server (e.g. ``ws://192.0.2.20:19101``).
    streaming_asr_ws_url: str | None = None
    # Per-incremental-result deadline before the streaming ASR is treated as
    # unavailable for graceful degradation (Req 5.2). Range 1-30s.
    streaming_asr_timeout_seconds: float = Field(default=5.0, ge=1, le=30)
    # How often the Web_Frontend transmits Audio_Increments; echoed to the
    # client in the start-live response so the timeslice is server-configured
    # (Req 3.1). Default 0.4s for lower preview latency; the 0.2s floor is a
    # tuning floor, not a recommended default (per-fragment ffmpeg/ASR overhead).
    streaming_send_interval_seconds: float = Field(default=0.4, ge=0.2, le=5)
    # Upper bound on the client's reconnect cadence after a dropped
    # Streaming_Connection (Req 5.5). Range 1-30s.
    streaming_reconnect_interval_seconds: float = Field(default=3.0, ge=1, le=30)
    # A live session with no Audio_Increment and no keep-alive for this long is
    # finalized as if ended (Req 7.3). Range 30-3600s, default 300s.
    live_inactivity_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    # Bounded sliding renewal: while actively streaming, the session expiry is
    # slid forward, capped at this absolute lifetime measured from session
    # establishment (Req 8.2). Range 300-86400s, default 4h.
    live_session_max_lifetime_seconds: int = Field(default=4 * 3600, ge=300, le=86400)
    # The WS handler re-validates the session at least this often so a
    # mid-connection logout/expiry closes the socket (Req 8.4). Range 5-600s.
    live_session_revalidation_interval_seconds: int = Field(default=60, ge=5, le=600)

    # --- Infra ------------------------------------------------------------
    # SQLite file on the shared volume, opened in WAL mode by API and worker.
    database_url: str = "sqlite:////data/db/app.db"
    redis_url: str  # required: Celery broker + cache (no sensible localhost default at deploy)
    audio_storage_dir: str = "/data/audio"

    # --- Registration -----------------------------------------------------
    # Whether unauthenticated visitors may self-register a (non-admin) account
    # via POST /api/auth/register. Defaults to False (closed); an Administrator
    # flips it at runtime through the admin config console without a restart.
    allow_public_registration: bool = False

    # --- Admin console (admin-console feature) ----------------------------
    # Symmetric key (urlsafe-base64, 32 bytes -> a Fernet key) used to encrypt
    # Configuration_Secrets (currently the LLM API key) at rest in runtime_config.
    # Optional until a runtime secret is actually stored; once one exists the
    # service fails fast at startup if this is absent/invalid. Supplied via
    # env/secret (CONFIG_SECRET_KEY) and NEVER committed. Generate one with
    # ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
    config_secret_key: str | None = None
    # How long the API/worker may serve a cached Effective_Configuration before
    # checking the persisted config version again (bounded staleness for the
    # no-restart propagation). Restart-only.
    config_refresh_seconds: int = Field(default=10, ge=1, le=300)

    # --- Celery / Job_Processor (task 9.1) --------------------------------
    # Coarse whole-task HARD cap (seconds). This is ONLY a safety net for a
    # runaway/hung task; it is NOT where the per-stage timeouts live (ffmpeg,
    # ASR httpx, and LLM httpx each enforce their own). Sized generously to be
    # roughly the sum of the stage budgets (design: "configured generously,
    # e.g. 3600s"). Runtime-configurable via CELERY_TASK_TIME_LIMIT_SECONDS.
    celery_task_time_limit_seconds: int = Field(default=3600, ge=1)
    # Optional SOFT cap (seconds), slightly below the hard cap. When set, Celery
    # raises ``SoftTimeLimitExceeded`` inside the task so it can fail gracefully
    # before the hard kill. ``None`` (default) disables the soft cap. Must be
    # ``< celery_task_time_limit_seconds`` to be meaningful.
    celery_task_soft_time_limit_seconds: int | None = Field(default=None, ge=1)
    # Bound worker concurrency so ffmpeg/ASR/LLM load stays predictable on the
    # shared CPU/GPU (design: "default 2"). Runtime-configurable via
    # CELERY_WORKER_CONCURRENCY; can also be overridden by the worker
    # ``--concurrency`` CLI flag at launch.
    celery_worker_concurrency: int = Field(default=2, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance.

    The instance is constructed on first call (reading the environment and any
    ``.env`` file) and memoized so the rest of the application shares one
    immutable view of configuration. Tests that mutate the environment can
    clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # required fields come from env/.env
