"""Tests for the runtime configuration store + registry (admin-console, task 3).

Verifies the persistence semantics the model-config API relies on:
- reset (``None``) deletes an override;
- secret fields are stored as ciphertext (not plaintext) and decrypt back;
- a batch write bumps the version exactly once;
- an unknown (non-registry) key is rejected and changes nothing;
- omitting a key leaves its existing override untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings
from app.config_store import (
    RUNTIME_EDITABLE,
    SECRET_KEYS,
    UnknownConfigKeyError,
    apply_changes,
    get_overrides,
    get_version,
)
from app.db import make_engine, make_session_factory
from app.models import RuntimeConfigEntry, create_all
from app.secrets import decrypt_secret, generate_key

KEY = generate_key()


def _settings() -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        config_secret_key=KEY,
    )  # type: ignore[call-arg]


@pytest.fixture()
def session() -> OrmSession:
    engine = make_engine("sqlite://")
    create_all(engine)
    factory = make_session_factory(engine)
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def test_registry_marks_only_llm_api_key_secret() -> None:
    assert SECRET_KEYS == frozenset({"llm_api_key"})
    assert "asr_endpoint_url" in RUNTIME_EDITABLE
    assert "database_url" not in RUNTIME_EDITABLE  # restart-only


def test_empty_store_has_version_zero_and_no_overrides(session: OrmSession) -> None:
    assert get_version(session) == 0
    assert get_overrides(session) == {}


def test_apply_changes_persists_and_bumps_version_once(session: OrmSession) -> None:
    v = apply_changes(
        session,
        {"asr_model_name": "paraformer", "asr_max_retries": 3, "llm_impl": "http"},
        actor_user_id=None,
        settings=_settings(),
    )
    session.commit()
    assert v == 1  # one batch -> single bump
    overrides = get_overrides(session)
    assert overrides["asr_model_name"] == "paraformer"
    assert overrides["asr_max_retries"] == "3"  # stringified
    assert overrides["llm_impl"] == "http"
    # A second batch bumps again by exactly one.
    v2 = apply_changes(session, {"asr_model_name": "sensevoice"}, settings=_settings())
    session.commit()
    assert v2 == 2


def test_secret_is_stored_encrypted_and_decrypts(session: OrmSession) -> None:
    apply_changes(
        session, {"llm_api_key": "sk-super-secret-value"}, settings=_settings()
    )
    session.commit()
    row = session.get(RuntimeConfigEntry, "llm_api_key")
    assert row is not None
    assert row.is_secret is True
    assert row.value != "sk-super-secret-value"  # ciphertext, not plaintext
    assert "sk-super-secret-value" not in row.value
    assert decrypt_secret(row.value, _settings()) == "sk-super-secret-value"


def test_reset_deletes_override(session: OrmSession) -> None:
    apply_changes(session, {"asr_model_name": "paraformer"}, settings=_settings())
    session.commit()
    assert "asr_model_name" in get_overrides(session)

    apply_changes(session, {"asr_model_name": None}, settings=_settings())
    session.commit()
    assert "asr_model_name" not in get_overrides(session)


def test_omitting_a_key_leaves_its_override_untouched(session: OrmSession) -> None:
    apply_changes(
        session,
        {"llm_api_key": "sk-1", "asr_model_name": "paraformer"},
        settings=_settings(),
    )
    session.commit()
    secret_before = session.get(RuntimeConfigEntry, "llm_api_key").value

    # A later change that does not mention llm_api_key must not alter it.
    apply_changes(session, {"asr_model_name": "sensevoice"}, settings=_settings())
    session.commit()
    secret_after = session.get(RuntimeConfigEntry, "llm_api_key").value
    assert secret_after == secret_before


def test_clearing_a_secret_persists_explicit_clear(session: OrmSession) -> None:
    apply_changes(session, {"llm_api_key": "sk-1"}, settings=_settings())
    session.commit()
    apply_changes(session, {"llm_api_key": None}, settings=_settings())
    session.commit()
    row = session.get(RuntimeConfigEntry, "llm_api_key")
    assert row is not None
    assert row.is_secret is True
    assert row.value == ""


def test_unknown_key_rejected_and_changes_nothing(session: OrmSession) -> None:
    with pytest.raises(UnknownConfigKeyError):
        apply_changes(
            session,
            {"database_url": "sqlite:///evil.db", "asr_model_name": "paraformer"},
            settings=_settings(),
        )
    session.rollback()
    # Neither the bad key nor the good key in the same batch was applied.
    assert get_overrides(session) == {}
    assert get_version(session) == 0
