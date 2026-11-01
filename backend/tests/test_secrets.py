"""Tests for Configuration_Secret encryption + startup fail-fast (admin-console).

Covers:
- Fernet round-trip and confidentiality (Property 4): ``decrypt(encrypt(x)) == x``
  for arbitrary text, and the ciphertext never contains the plaintext.
- ``get_fernet`` raises ``SecretKeyError`` when the key is absent or malformed.
- ``check_runtime_secrets`` (Req 4.6): no-op when no secret rows exist; aborts
  with a ``ConfigError`` when a secret row exists without a usable key or when a
  stored secret cannot be decrypted with the current key.

Settings are built directly (no environment dependency); the schema is created
on a fresh in-memory SQLite engine.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import RuntimeConfigEntry, create_all
from app.secrets import (
    SecretDecryptError,
    SecretKeyError,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    get_fernet,
)
from app.startup import ConfigError, check_runtime_secrets

KEY_A = generate_key()
KEY_B = generate_key()


def _settings(*, config_secret_key: str | None) -> Settings:
    return Settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_password="pw",
        redis_url="redis://localhost:6379/0",
        config_secret_key=config_secret_key,
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


# --- round-trip + confidentiality (Property 4) -------------------------------


@hyp_settings(max_examples=200)
@given(plaintext=st.text(min_size=0, max_size=400))
def test_fernet_round_trip(plaintext: str) -> None:
    """decrypt(encrypt(x)) == x for arbitrary text; the token is not the plaintext."""
    s = _settings(config_secret_key=KEY_A)
    token = encrypt_secret(plaintext, s)
    assert decrypt_secret(token, s) == plaintext
    # The stored token is never the plaintext itself.
    assert token != plaintext


def test_ciphertext_does_not_contain_a_realistic_secret() -> None:
    """A realistic API key does not appear verbatim inside its ciphertext.

    (Asserted for a specific long secret; short strings can coincidentally occur
    in base64, so the general round-trip property above only checks inequality.)
    """
    secret = "sk-deepseek-1234567890ABCDEFghij-very-secret-key"
    token = encrypt_secret(secret, _settings(config_secret_key=KEY_A))
    assert secret not in token


def test_encrypt_is_nondeterministic_but_decrypts() -> None:
    """Two encryptions of the same value differ (Fernet IV) yet both decrypt."""
    s = _settings(config_secret_key=KEY_A)
    t1 = encrypt_secret("sk-secret", s)
    t2 = encrypt_secret("sk-secret", s)
    assert t1 != t2
    assert decrypt_secret(t1, s) == "sk-secret"
    assert decrypt_secret(t2, s) == "sk-secret"


# --- key validation ----------------------------------------------------------


def test_get_fernet_requires_a_key() -> None:
    with pytest.raises(SecretKeyError):
        get_fernet(_settings(config_secret_key=None))


def test_get_fernet_rejects_malformed_key() -> None:
    with pytest.raises(SecretKeyError):
        get_fernet(_settings(config_secret_key="not-a-valid-fernet-key"))


def test_decrypt_with_wrong_key_raises() -> None:
    token = encrypt_secret("sk-secret", _settings(config_secret_key=KEY_A))
    with pytest.raises(SecretDecryptError):
        decrypt_secret(token, _settings(config_secret_key=KEY_B))


# --- startup fail-fast (Req 4.6) ---------------------------------------------


def test_check_runtime_secrets_noop_without_secret_rows(session: OrmSession) -> None:
    # A non-secret override present, no key configured -> still a no-op.
    session.add(RuntimeConfigEntry(key="llm_model_name", value="m", is_secret=False))
    session.commit()
    check_runtime_secrets(session)  # must not raise


def test_check_runtime_secrets_noop_for_explicit_clear(
    session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(RuntimeConfigEntry(key="llm_api_key", value="", is_secret=True))
    session.commit()
    monkeypatch.setattr(
        "app.secrets.get_settings", lambda: _settings(config_secret_key=None)
    )
    check_runtime_secrets(session)  # an explicit clear needs no decryption key


def test_check_runtime_secrets_aborts_without_key(
    session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(
        RuntimeConfigEntry(
            key="llm_api_key", value=encrypt_secret("k", _settings(config_secret_key=KEY_A)), is_secret=True
        )
    )
    session.commit()
    # No CONFIG_SECRET_KEY in effect.
    monkeypatch.setattr(
        "app.secrets.get_settings", lambda: _settings(config_secret_key=None)
    )
    with pytest.raises(ConfigError) as ei:
        check_runtime_secrets(session)
    assert any("CONFIG_SECRET_KEY" in p for p in ei.value.problems)


def test_check_runtime_secrets_ok_with_matching_key(
    session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(
        RuntimeConfigEntry(
            key="llm_api_key",
            value=encrypt_secret("k", _settings(config_secret_key=KEY_A)),
            is_secret=True,
        )
    )
    session.commit()
    monkeypatch.setattr(
        "app.secrets.get_settings", lambda: _settings(config_secret_key=KEY_A)
    )
    check_runtime_secrets(session)  # decrypts cleanly -> no raise


def test_check_runtime_secrets_aborts_on_undecryptable_secret(
    session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stored under KEY_A but the process is configured with KEY_B.
    session.add(
        RuntimeConfigEntry(
            key="llm_api_key",
            value=encrypt_secret("k", _settings(config_secret_key=KEY_A)),
            is_secret=True,
        )
    )
    session.commit()
    monkeypatch.setattr(
        "app.secrets.get_settings", lambda: _settings(config_secret_key=KEY_B)
    )
    with pytest.raises(ConfigError) as ei:
        check_runtime_secrets(session)
    assert any("cannot be decrypted" in p for p in ei.value.problems)
