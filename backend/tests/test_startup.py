"""Unit tests for fail-fast startup validation (task 1.3).

Exercises the cross-field / conditional validation in :mod:`app.startup`:

- HTTP mode with a missing/invalid ASR or LLM URL or missing model name reports
  every offending key.
- Mock mode tolerates absent endpoint URLs / model name (Req 11.5).
- Malformed absolute URLs are rejected.
- A fully valid HTTP-mode configuration passes.

The dedicated property-based test for Property 27 is task 1.4; these are the
focused example/edge-case unit tests for the validation API.
"""

from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.startup import (
    ConfigError,
    load_and_validate_settings,
    load_settings_or_exit,
    validate_settings,
)

# Required fields (no defaults) needed to construct a Settings at all.
REQUIRED_ENV = {
    "bootstrap_admin_username": "admin",
    "bootstrap_admin_password": "s3cret-pass",
    "redis_url": "redis://localhost:6379/0",
}


def _make_settings(**overrides: object) -> Settings:
    """Construct a Settings from explicit values, ignoring any local .env."""
    data = {**REQUIRED_ENV, **overrides}
    return Settings(_env_file=None, **data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# validate_settings: HTTP mode requirements
# --------------------------------------------------------------------------


def test_http_mode_missing_all_required_lists_every_offending_key() -> None:
    """HTTP mode with no URLs / model name reports all three offending keys."""
    settings = _make_settings(
        asr_impl="http",
        llm_impl="http",
        asr_endpoint_url=None,
        llm_endpoint_url=None,
        llm_model_name=None,
    )
    with pytest.raises(ConfigError) as exc_info:
        validate_settings(settings)

    joined = "\n".join(exc_info.value.problems)
    assert "asr_endpoint_url" in joined
    assert "llm_endpoint_url" in joined
    assert "llm_model_name" in joined
    # Every offending key surfaced, not just the first.
    assert len(exc_info.value.problems) == 3


def test_http_mode_missing_only_llm_model_name() -> None:
    """A present URL but missing model name still fails on the model name."""
    settings = _make_settings(
        asr_impl="http",
        llm_impl="http",
        asr_endpoint_url="http://asr.local:9000/asr",
        llm_endpoint_url="http://llm.local:8000/v1/chat/completions",
        llm_model_name=None,
    )
    with pytest.raises(ConfigError) as exc_info:
        validate_settings(settings)

    assert len(exc_info.value.problems) == 1
    assert "llm_model_name" in exc_info.value.problems[0]


def test_http_mode_blank_model_name_is_rejected() -> None:
    """A whitespace-only model name counts as missing."""
    settings = _make_settings(
        asr_impl="http",
        llm_impl="http",
        asr_endpoint_url="http://asr.local:9000/asr",
        llm_endpoint_url="http://llm.local:8000/v1",
        llm_model_name="   ",
    )
    with pytest.raises(ConfigError) as exc_info:
        validate_settings(settings)

    assert any("llm_model_name" in p for p in exc_info.value.problems)


def test_valid_full_http_config_passes() -> None:
    """A complete, well-formed HTTP-mode configuration validates cleanly."""
    settings = _make_settings(
        asr_impl="http",
        llm_impl="http",
        asr_endpoint_url="http://192.0.2.20:9000/asr",
        llm_endpoint_url="http://192.0.2.20:8000/v1/chat/completions",
        llm_model_name="qwen2.5",
    )
    # Should not raise.
    assert validate_settings(settings) is None


# --------------------------------------------------------------------------
# validate_settings: mock-mode tolerance (Req 11.5)
# --------------------------------------------------------------------------


def test_mock_mode_allows_absent_urls_and_model_name() -> None:
    """Mock mode tolerates missing endpoint URLs and model name."""
    settings = _make_settings(
        asr_impl="mock",
        llm_impl="mock",
        asr_endpoint_url=None,
        llm_endpoint_url=None,
        llm_model_name=None,
    )
    assert validate_settings(settings) is None


def test_mixed_mock_asr_http_llm() -> None:
    """Mock ASR ignores its URL; HTTP LLM still enforces its requirements."""
    settings = _make_settings(
        asr_impl="mock",
        llm_impl="http",
        asr_endpoint_url=None,
        llm_endpoint_url=None,
        llm_model_name=None,
    )
    with pytest.raises(ConfigError) as exc_info:
        validate_settings(settings)

    joined = "\n".join(exc_info.value.problems)
    assert "asr_endpoint_url" not in joined  # mock ASR not required
    assert "llm_endpoint_url" in joined
    assert "llm_model_name" in joined


# --------------------------------------------------------------------------
# load_and_validate_settings: malformed URLs (caught by pydantic typing)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["not-a-url", "/relative", "ftp://"])
def test_malformed_asr_url_is_rejected(bad_url: str) -> None:
    """A malformed absolute URL aborts via the load-and-validate path."""
    with pytest.raises(ConfigError) as exc_info:
        load_and_validate_settings(
            _env_file=None,
            asr_impl="http",
            llm_impl="mock",
            asr_endpoint_url=bad_url,
            **REQUIRED_ENV,
        )
    assert any("asr_endpoint_url" in p for p in exc_info.value.problems)


def test_load_and_validate_aggregates_pydantic_and_cross_field_errors() -> None:
    """Out-of-range numbers (pydantic) are surfaced as ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        load_and_validate_settings(
            _env_file=None,
            asr_impl="mock",
            llm_impl="mock",
            asr_max_retries=99,  # above le=5 -> pydantic ValidationError
            **REQUIRED_ENV,
        )
    assert any("asr_max_retries" in p for p in exc_info.value.problems)


def test_load_and_validate_valid_config_returns_settings() -> None:
    """A valid configuration returns a constructed Settings instance."""
    settings = load_and_validate_settings(
        _env_file=None,
        asr_impl="mock",
        llm_impl="mock",
        **REQUIRED_ENV,
    )
    assert isinstance(settings, Settings)
    assert settings.asr_impl == "mock"


# --------------------------------------------------------------------------
# load_settings_or_exit: fail-fast wrapper
# --------------------------------------------------------------------------


def test_load_settings_or_exit_aborts_nonzero_and_logs_every_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On violation, every offending key is logged and the process exits 1."""
    with caplog.at_level(logging.ERROR, logger="app.startup"):
        with pytest.raises(SystemExit) as exc_info:
            load_settings_or_exit(
                _env_file=None,
                asr_impl="http",
                llm_impl="http",
                asr_endpoint_url=None,
                llm_endpoint_url=None,
                llm_model_name=None,
                **REQUIRED_ENV,
            )

    assert exc_info.value.code == 1
    log_text = caplog.text
    assert "asr_endpoint_url" in log_text
    assert "llm_endpoint_url" in log_text
    assert "llm_model_name" in log_text


def test_load_settings_or_exit_returns_settings_when_valid() -> None:
    """The fail-fast wrapper returns Settings for a valid configuration."""
    settings = load_settings_or_exit(
        _env_file=None,
        asr_impl="mock",
        llm_impl="mock",
        **REQUIRED_ENV,
    )
    assert isinstance(settings, Settings)
