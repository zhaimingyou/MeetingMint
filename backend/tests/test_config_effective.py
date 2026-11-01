"""Tests for the Effective_Configuration provider (admin-console, task 4).

Covers:
- Property 2: empty overrides => Effective_Configuration equals the base
  Settings; non-secret/secret overrides are applied; unknown/invalid overrides
  are ignored with a fallback to base.
- Property 5: the provider serves a cached value within the TTL and rebuilds
  after the TTL elapses when the persisted version changed.
- Property 6: the worker resolves the Effective_Configuration at each stage
  start, so a change made before a stage is observed by that stage (an in-flight
  stage keeps the config it began with).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

import app.config_effective as ce
import app.worker.tasks as tasks_mod
from app.config import Settings
from app.config_effective import (
    ConfigProvider,
    effective_settings_for,
    overlay_settings,
    reset_config_provider,
)
from app.config_store import apply_changes
from app.db import make_engine, make_session_factory
from app.enums import JobStatus
from app.models import MeetingRecord, ProcessingJob, create_all
from app.secrets import generate_key

KEY = generate_key()


def _base(**overrides: object) -> Settings:
    fields: dict[str, object] = dict(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        database_url="sqlite://",
        asr_impl="mock",
        llm_impl="mock",
        config_secret_key=KEY,
    )
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


@pytest.fixture()
def sf() -> sessionmaker[OrmSession]:
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    reset_config_provider()
    yield
    reset_config_provider()


# --- overlay (Property 2) ----------------------------------------------------


def test_overlay_empty_returns_base_identity() -> None:
    base = _base()
    assert overlay_settings(base, {}) is base


def test_overlay_applies_non_secret_overrides() -> None:
    base = _base()
    eff = overlay_settings(
        base, {"asr_model_name": "paraformer", "llm_impl": "http", "asr_max_retries": "4"}
    )
    assert eff.asr_model_name == "paraformer"
    assert eff.llm_impl == "http"
    assert eff.asr_max_retries == 4  # coerced int
    # The base is untouched.
    assert base.asr_model_name == "sensevoice"


def test_overlay_decrypts_secret_override() -> None:
    from app.secrets import encrypt_secret

    base = _base()
    token = encrypt_secret("sk-live-secret", base)
    eff = overlay_settings(base, {"llm_api_key": token})
    assert eff.llm_api_key == "sk-live-secret"


def test_explicit_clear_overrides_environment_secret() -> None:
    base = _base(llm_api_key="sk-from-env")
    eff = overlay_settings(base, {"llm_api_key": ""})
    assert eff.llm_api_key is None


def test_overlay_ignores_unknown_key() -> None:
    base = _base()
    eff = overlay_settings(base, {"database_url": "sqlite:///evil.db"})
    assert eff is base  # nothing applicable -> base returned unchanged


def test_overlay_falls_back_to_base_on_invalid_value() -> None:
    base = _base()
    # 999 exceeds asr_max_retries' le=5 bound -> Settings construction fails ->
    # the whole overlay falls back to base rather than taking the service down.
    eff = overlay_settings(base, {"asr_max_retries": "999"})
    assert eff.asr_max_retries == base.asr_max_retries


def test_effective_settings_for_overlays_from_db(sf: sessionmaker[OrmSession]) -> None:
    base = _base()
    session = sf()
    try:
        apply_changes(session, {"asr_model_name": "paraformer"}, settings=base)
        session.commit()
        eff = effective_settings_for(base, session)
    finally:
        session.close()
    assert eff.asr_model_name == "paraformer"


# --- provider caching + version reload (Property 5) --------------------------


def test_provider_caches_within_ttl_then_reloads_on_version_change(
    sf: sessionmaker[OrmSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _base(config_refresh_seconds=10)
    monkeypatch.setattr(ce, "get_settings", lambda: base)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ce.time, "monotonic", lambda: clock["t"])

    provider = ConfigProvider()

    # First resolution: no overrides -> base value.
    assert provider.effective(sf).asr_model_name == "sensevoice"

    # Change the override and bump the version.
    session = sf()
    try:
        apply_changes(session, {"asr_model_name": "paraformer"}, settings=base)
        session.commit()
    finally:
        session.close()

    # Within the TTL window the provider serves the cached value (no reload).
    assert provider.effective(sf).asr_model_name == "sensevoice"

    # Advance the clock past the TTL: the provider re-checks the version, sees it
    # changed, and rebuilds.
    clock["t"] += 11.0
    assert provider.effective(sf).asr_model_name == "paraformer"


# --- worker stage isolation / per-stage resolution (Property 6) --------------


def test_worker_resolves_effective_config_at_each_stage_start(
    sf: sessionmaker[OrmSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _base(config_refresh_seconds=10)
    monkeypatch.setattr(ce, "get_settings", lambda: base)

    # Seed a queued meeting/job.
    meeting_id = "m-stage-iso"
    session = sf()
    try:
        from app.models import User

        owner = User(username="stage-owner", password_hash="x")
        session.add(owner)
        session.flush()
        session.add(
            MeetingRecord(
                id=meeting_id,
                owner_id=owner.id,
                title="t",
                source_audio_path="/tmp/src.webm",
                source_format="webm",
            )
        )
        session.add(ProcessingJob(meeting_id=meeting_id, status=JobStatus.QUEUED.value))
        session.commit()
    finally:
        session.close()

    captured: dict[str, object] = {}

    class _FakeAsr:
        def transcribe(self, path: str):  # noqa: ANN201
            return []

    class _FakeLlm:
        def generate_minutes(self, transcript):  # noqa: ANN201
            return {"summary": "s", "topics": [], "decisions": [], "action_items": []}

    def _fake_make_asr(settings: Settings):  # noqa: ANN202
        captured["asr_model_name"] = settings.asr_model_name
        return _FakeAsr()

    def _fake_make_llm(settings: Settings):  # noqa: ANN202
        captured["llm_model_name"] = settings.llm_model_name
        return _FakeLlm()

    monkeypatch.setattr(tasks_mod, "make_asr_client", _fake_make_asr)
    monkeypatch.setattr(tasks_mod, "make_llm_client", _fake_make_llm)

    def _apply(changes: dict[str, object]) -> None:
        s = sf()
        try:
            apply_changes(s, changes, settings=base)
            s.commit()
        finally:
            s.close()
        # Make the change immediately visible (bypass the TTL) for the test.
        reset_config_provider()

    def _observer(status: JobStatus) -> None:
        # Change config BEFORE the next stage resolves it.
        if status is JobStatus.PREPROCESSING:
            _apply({"asr_model_name": "paraformer"})
        elif status is JobStatus.TRANSCRIBING:
            _apply({"llm_model_name": "llm-v2"})

    result = tasks_mod.run_pipeline(
        meeting_id,
        session_factory=sf,
        preprocess_fn=lambda src, out, timeout_seconds=None: out,
        status_observer=_observer,
    )

    assert result is JobStatus.COMPLETED
    # The transcribe stage observed the change applied during preprocessing, and
    # the generate stage observed the change applied during transcribing -- each
    # stage resolves the Effective_Configuration at its own start.
    assert captured["asr_model_name"] == "paraformer"
    assert captured["llm_model_name"] == "llm-v2"
