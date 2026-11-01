"""Property-based tests for startup configuration validation (task 1.4).

These complement the focused example/edge-case unit tests in
``tests/test_startup.py`` and ``tests/test_config.py`` by exercising the
fail-fast validation in :mod:`app.startup` across a wide input space with
``hypothesis`` (>= 100 examples each).

Primary coverage is design **Property 27** (Req 10.4, 10.5): startup must abort
when any required HTTP-mode configuration value is absent (naming every absent
value) or when a configured endpoint URL is not a well-formed absolute URL
(identifying that value). A supplemental property covers mock-mode tolerance of
absent endpoint URLs / model name (Req 11.5).
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.config import Settings
from app.startup import ConfigError, load_and_validate_settings

# Required fields (no defaults) needed to construct a Settings at all. Keyed by
# the Settings field names so they can be passed as constructor kwargs.
REQUIRED_KWARGS = {
    "bootstrap_admin_username": "admin",
    "bootstrap_admin_password": "s3cret-pass",
    "redis_url": "redis://localhost:6379/0",
}

# The required configuration values that matter in HTTP mode, grouped by the
# impl selector that makes them relevant.
_ASR_HTTP_KEYS = ("asr_endpoint_url",)
_LLM_HTTP_KEYS = ("llm_endpoint_url", "llm_model_name")


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------


@st.composite
def valid_http_urls(draw: st.DrawFn) -> str:
    """Generate well-formed absolute HTTP/HTTPS URLs (scheme + host)."""
    scheme = draw(st.sampled_from(["http", "https"]))
    host = draw(st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True))
    port = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=65535)))
    path = draw(st.from_regex(r"(/[a-z0-9]{1,8}){0,3}", fullmatch=True))
    netloc = host if port is None else f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"


# Non-empty model names that survive ``.strip()`` (so they count as present).
valid_model_names = st.from_regex(r"[A-Za-z][A-Za-z0-9._-]{0,30}", fullmatch=True)

# Strings that are NOT well-formed absolute URLs. The curated values cover the
# obvious shapes (no scheme, relative, missing host, wrong scheme); the
# generated text excludes ``:`` ``/`` ``.`` so it can never form a scheme/host.
malformed_urls = st.one_of(
    st.sampled_from(
        [
            "not-a-url",
            "/relative/path",
            "ftp://",
            "http://",
            "https://",
            "://missing-scheme",
            "just some text",
            "www.example.com",
            "12345",
            "file://x",
        ]
    ),
    st.text(
        alphabet=st.characters(
            min_codepoint=97,
            max_codepoint=122,  # lowercase ascii letters only
        ),
        min_size=1,
        max_size=20,
    ),
)


# --------------------------------------------------------------------------
# Property 27 -- clause 1: absent required HTTP-mode values abort startup and
# every absent value is named.
# --------------------------------------------------------------------------


# Feature: meeting-minutes-service, Property 27: For any non-empty subset of the required configuration values (ASR endpoint URL, LLM endpoint URL, LLM model name, in HTTP mode) that is absent at startup, the Backend_API aborts startup and logs an error naming each absent value; and for any configured endpoint URL that is not a well-formed absolute URL, the Backend_API aborts startup and logs an error identifying that value.
# Validates: Requirements 10.4, 10.5
@settings(max_examples=100)
@given(
    asr_impl=st.sampled_from(["http", "mock"]),
    llm_impl=st.sampled_from(["http", "mock"]),
    absent_asr_url=st.booleans(),
    absent_llm_url=st.booleans(),
    absent_llm_model=st.booleans(),
    present_url=valid_http_urls(),
    present_model=valid_model_names,
)
def test_absent_required_http_config_aborts_and_names_each(
    asr_impl: str,
    llm_impl: str,
    absent_asr_url: bool,
    absent_llm_url: bool,
    absent_llm_model: bool,
    present_url: str,
    present_model: str,
) -> None:
    """A non-empty subset of absent required HTTP-mode values aborts startup,
    and every absent (relevant) key is surfaced in the aggregated problems.
    """
    # Determine which keys are *relevant* (required) given the active impls,
    # and which of those the scenario marks as absent.
    relevant: list[str] = []
    absent: set[str] = set()
    if asr_impl == "http":
        relevant += _ASR_HTTP_KEYS
        if absent_asr_url:
            absent.add("asr_endpoint_url")
    if llm_impl == "http":
        relevant += _LLM_HTTP_KEYS
        if absent_llm_url:
            absent.add("llm_endpoint_url")
        if absent_llm_model:
            absent.add("llm_model_name")

    # Property 27 ranges over a NON-EMPTY subset of absent required values.
    assume(absent)

    overrides: dict[str, object] = {"asr_impl": asr_impl, "llm_impl": llm_impl}
    # ASR endpoint URL: present-and-valid unless explicitly absent (only
    # relevant when asr_impl == "http"; harmless None otherwise).
    overrides["asr_endpoint_url"] = (
        None if "asr_endpoint_url" in absent or asr_impl != "http" else present_url
    )
    # LLM endpoint URL + model name follow the same rule under llm_impl.
    overrides["llm_endpoint_url"] = (
        None if "llm_endpoint_url" in absent or llm_impl != "http" else present_url
    )
    overrides["llm_model_name"] = (
        None if "llm_model_name" in absent or llm_impl != "http" else present_model
    )

    with pytest.raises(ConfigError) as exc_info:
        load_and_validate_settings(_env_file=None, **overrides, **REQUIRED_KWARGS)

    problems_text = "\n".join(exc_info.value.problems)
    # Every absent required key for the active impl must be named.
    for key in absent:
        assert key in problems_text, f"{key} not named in problems: {problems_text!r}"
    # Keys that are NOT relevant (mock impl) must never be reported as missing.
    if asr_impl != "http":
        assert "asr_endpoint_url" not in problems_text
    if llm_impl != "http":
        assert "llm_endpoint_url" not in problems_text
        assert "llm_model_name" not in problems_text


# --------------------------------------------------------------------------
# Property 27 -- clause 2: a configured endpoint URL that is not a well-formed
# absolute URL aborts startup and identifies that value.
# --------------------------------------------------------------------------


# Feature: meeting-minutes-service, Property 27: For any non-empty subset of the required configuration values (ASR endpoint URL, LLM endpoint URL, LLM model name, in HTTP mode) that is absent at startup, the Backend_API aborts startup and logs an error naming each absent value; and for any configured endpoint URL that is not a well-formed absolute URL, the Backend_API aborts startup and logs an error identifying that value.
# Validates: Requirements 10.4, 10.5
@settings(max_examples=100)
@given(bad_url=malformed_urls, target=st.sampled_from(["asr", "llm"]))
def test_malformed_endpoint_url_aborts_and_identifies_value(
    bad_url: str, target: str
) -> None:
    """A malformed (non well-formed absolute) endpoint URL aborts startup with
    the offending key identified. Malformed URLs are caught at Settings
    construction, so this is driven via ``load_and_validate_settings``.
    """
    if target == "asr":
        overrides: dict[str, object] = {
            "asr_impl": "http",
            "llm_impl": "mock",
            "asr_endpoint_url": bad_url,
        }
        offending_key = "asr_endpoint_url"
    else:
        overrides = {
            "asr_impl": "mock",
            "llm_impl": "http",
            "llm_endpoint_url": bad_url,
            "llm_model_name": "qwen2.5",
        }
        offending_key = "llm_endpoint_url"

    with pytest.raises(ConfigError) as exc_info:
        load_and_validate_settings(_env_file=None, **overrides, **REQUIRED_KWARGS)

    assert any(offending_key in p for p in exc_info.value.problems), (
        f"{offending_key} not identified for bad_url={bad_url!r}: "
        f"{exc_info.value.problems!r}"
    )


# --------------------------------------------------------------------------
# Supplemental property: mock-mode tolerance of absent model values (Req 11.5).
# --------------------------------------------------------------------------


# Validates: Requirements 11.5
@settings(max_examples=100)
@given(
    asr_url=st.one_of(st.none(), valid_http_urls()),
    llm_url=st.one_of(st.none(), valid_http_urls()),
    model_name=st.one_of(st.none(), valid_model_names),
)
def test_mock_mode_tolerates_absent_model_values(
    asr_url: str | None, llm_url: str | None, model_name: str | None
) -> None:
    """In full mock mode, validation passes regardless of whether the endpoint
    URLs / model name are present or absent.
    """
    settings_obj = load_and_validate_settings(
        _env_file=None,
        asr_impl="mock",
        llm_impl="mock",
        asr_endpoint_url=asr_url,
        llm_endpoint_url=llm_url,
        llm_model_name=model_name,
        **REQUIRED_KWARGS,
    )
    assert isinstance(settings_obj, Settings)
    assert settings_obj.asr_impl == "mock"
    assert settings_obj.llm_impl == "mock"
