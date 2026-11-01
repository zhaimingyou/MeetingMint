"""Unit tests for the Configuration_Store (`app.config.Settings`).

These verify the settings schema declared in task 1.2: required fields, the
documented defaults, basic type coercion, and the `Field` range constraints.
Cross-field / fail-fast startup validation (task 1.3) is intentionally NOT
exercised here.
"""

from __future__ import annotations

import pytest
from pydantic import AnyHttpUrl, ValidationError

from app.config import Settings, get_settings

# Minimal set of required env vars (fields without defaults).
REQUIRED_ENV = {
    "BOOTSTRAP_ADMIN_USERNAME": "admin",
    "BOOTSTRAP_ADMIN_PASSWORD": "s3cret-pass",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_required_fields_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the required env vars, instantiation fails validation."""
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    # Avoid reading a stray local .env file during the test.
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_present_with_required_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """All documented defaults are applied when only required vars are set."""
    _set_required(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    # Required values come from the environment.
    assert settings.bootstrap_admin_username == "admin"
    assert settings.bootstrap_admin_password == "s3cret-pass"
    assert settings.redis_url == "redis://localhost:6379/0"

    # Auth defaults.
    assert settings.session_ttl_minutes == 60
    assert settings.max_credential_length == 256

    # Uploads defaults.
    assert settings.max_upload_bytes == 500 * 1024 * 1024
    assert settings.supported_formats == ["mp3", "wav", "m4a", "webm"]

    # Export default.
    assert settings.export_format == "markdown"

    # Preprocessing default.
    assert settings.preprocess_timeout_seconds == 600

    # ASR client defaults.
    assert settings.asr_impl == "http"
    assert settings.asr_endpoint_url is None
    assert settings.asr_model_name == "sensevoice"
    assert settings.asr_request_timeout_seconds == 120
    assert settings.asr_chunk_length_seconds == 300
    assert settings.asr_max_retries == 2

    # LLM client defaults.
    assert settings.llm_impl == "http"
    assert settings.llm_endpoint_url is None
    assert settings.llm_model_name is None
    assert settings.llm_api_key is None
    assert settings.llm_request_timeout_seconds == 120

    # Infra defaults.
    assert settings.database_url == "sqlite:////data/db/app.db"
    assert settings.audio_storage_dir == "/data/audio"

    # Celery / Job_Processor defaults (task 9.1).
    assert settings.celery_task_time_limit_seconds == 3600
    assert settings.celery_task_soft_time_limit_seconds is None
    assert settings.celery_worker_concurrency == 2


def test_types_are_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Field types coerce/validate as declared."""
    _set_required(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert isinstance(settings.session_ttl_minutes, int)
    assert isinstance(settings.max_upload_bytes, int)
    assert isinstance(settings.supported_formats, list)
    assert all(isinstance(fmt, str) for fmt in settings.supported_formats)
    assert isinstance(settings.database_url, str)
    assert isinstance(settings.redis_url, str)


def test_env_overrides_and_url_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars override defaults; URL fields parse into AnyHttpUrl."""
    _set_required(monkeypatch)
    monkeypatch.setenv("ASR_IMPL", "mock")
    monkeypatch.setenv("LLM_IMPL", "mock")
    monkeypatch.setenv("ASR_ENDPOINT_URL", "http://192.0.2.20:9000/asr")
    monkeypatch.setenv("LLM_MODEL_NAME", "qwen2.5")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.asr_impl == "mock"
    assert settings.llm_impl == "mock"
    assert isinstance(settings.asr_endpoint_url, AnyHttpUrl)
    assert settings.llm_model_name == "qwen2.5"
    assert settings.max_upload_bytes == 1024


@pytest.mark.parametrize(
    ("env_key", "bad_value"),
    [
        ("PREPROCESS_TIMEOUT_SECONDS", "0"),  # below ge=1
        ("PREPROCESS_TIMEOUT_SECONDS", "601"),  # above le=600
        ("ASR_REQUEST_TIMEOUT_SECONDS", "601"),  # above le=600
        ("ASR_CHUNK_LENGTH_SECONDS", "29"),  # below ge=30
        ("ASR_CHUNK_LENGTH_SECONDS", "601"),  # above le=600
        ("ASR_MAX_RETRIES", "6"),  # above le=5
        ("CELERY_TASK_TIME_LIMIT_SECONDS", "0"),  # below ge=1
        ("CELERY_WORKER_CONCURRENCY", "0"),  # below ge=1
        ("CELERY_TASK_SOFT_TIME_LIMIT_SECONDS", "0"),  # below ge=1
    ],
)
def test_numeric_range_constraints(
    monkeypatch: pytest.MonkeyPatch, env_key: str, bad_value: str
) -> None:
    """Out-of-range numeric values fail validation per design ranges."""
    _set_required(monkeypatch)
    monkeypatch.setenv(env_key, bad_value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_settings() memoizes a single instance until the cache is cleared."""
    _set_required(monkeypatch)
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
