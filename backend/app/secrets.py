"""Configuration_Secret encryption at rest (admin-console, task 2).

Runtime configuration secrets -- currently only the LLM API key -- are stored
**encrypted** in the ``runtime_config`` table, never in plaintext (Req 4.1). This
module is the single authority for that encryption: symmetric Fernet (AES-128-CBC
+ HMAC, authenticated) keyed by :attr:`app.config.Settings.config_secret_key`
(env ``CONFIG_SECRET_KEY``), a urlsafe-base64 32-byte Fernet key.

Design choices
--------------
- **Symmetric is sufficient.** The key lives in the deploy environment, the same
  trust boundary as today's plaintext ``.env`` values; encrypting at rest defends
  against a leaked database file, not against the operator.
- **Authenticated.** Fernet bundles an HMAC, so a tampered/corrupted ciphertext
  (or one written under a different key) fails closed with
  :class:`SecretDecryptError` rather than yielding garbage.
- **Never logs the plaintext or the key.** Errors carry only generic messages.

The key is resolved lazily per call so a process that never touches a secret
(e.g. an all-mock deployment with no stored API key) need not configure
``CONFIG_SECRET_KEY`` at all. The startup fail-fast in :mod:`app.startup` makes
the requirement appear only when an encrypted secret actually exists (Req 4.6).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings, get_settings

__all__ = [
    "SecretKeyError",
    "SecretDecryptError",
    "get_fernet",
    "encrypt_secret",
    "decrypt_secret",
    "generate_key",
]


class SecretKeyError(RuntimeError):
    """Raised when ``CONFIG_SECRET_KEY`` is absent or not a valid Fernet key."""


class SecretDecryptError(RuntimeError):
    """Raised when a stored Configuration_Secret cannot be decrypted.

    Indicates the ciphertext was written under a different key or is corrupted.
    Carries no secret material.
    """


def _resolve_key(settings: Settings | None) -> str:
    key = (settings or get_settings()).config_secret_key
    if not key:
        raise SecretKeyError(
            "CONFIG_SECRET_KEY is not set; cannot encrypt or decrypt "
            "configuration secrets"
        )
    return key


def get_fernet(settings: Settings | None = None) -> Fernet:
    """Build a :class:`~cryptography.fernet.Fernet` from the configured key.

    :raises SecretKeyError: when the key is absent or malformed (Fernet requires
        a 32-byte urlsafe-base64 key). The exception message never includes the
        key value.
    """
    key = _resolve_key(settings)
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretKeyError(
            "CONFIG_SECRET_KEY is not a valid Fernet key (expected a 32-byte "
            "urlsafe-base64 value)"
        ) from exc


def encrypt_secret(plaintext: str, settings: Settings | None = None) -> str:
    """Encrypt ``plaintext`` to an ASCII token for storage in ``runtime_config``.

    :raises SecretKeyError: when ``CONFIG_SECRET_KEY`` is absent/invalid.
    """
    return get_fernet(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, settings: Settings | None = None) -> str:
    """Decrypt a stored token back to its plaintext.

    :raises SecretKeyError: when ``CONFIG_SECRET_KEY`` is absent/invalid.
    :raises SecretDecryptError: when the token cannot be decrypted with the
        current key (wrong key or corrupted value).
    """
    try:
        return get_fernet(settings).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptError(
            "failed to decrypt a configuration secret (wrong CONFIG_SECRET_KEY "
            "or corrupted value)"
        ) from exc


def generate_key() -> str:
    """Return a fresh urlsafe-base64 Fernet key (for operator key generation)."""
    return Fernet.generate_key().decode("ascii")
