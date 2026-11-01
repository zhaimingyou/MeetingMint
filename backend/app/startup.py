"""Fail-fast startup configuration validation (task 1.3).

This module implements the design's "fail-fast on missing/invalid values"
startup contract for the Configuration_Store (Req 10.4 / 10.5, and the mock-mode
tolerance of Req 11.5). It layers conditional, cross-field validation on top of
the plain type/range checks that :class:`app.config.Settings` already performs at
construction:

- **HTTP mode requires endpoints.** When ``asr_impl == "http"`` the
  ``asr_endpoint_url`` must be set; when ``llm_impl == "http"`` both
  ``llm_endpoint_url`` and a non-blank ``llm_model_name`` must be set. The URLs'
  *well-formedness* (absolute, scheme + host) is enforced by the ``AnyHttpUrl``
  typing in :class:`~app.config.Settings`, so a malformed URL fails at
  construction; this module additionally enforces *presence* for the active
  implementation.
- **Mock mode tolerates absence.** When an implementation is ``"mock"`` its
  endpoint URL / model name may be absent -- the system runs model-less
  (Req 11.5).
- **Every offending key is reported.** Validation never stops at the first
  problem: :class:`ConfigError` carries the full list so an operator sees all
  misconfigured keys at once.

Three layers of API
-------------------
- :func:`validate_settings` -- pure cross-field validation of an existing
  :class:`~app.config.Settings`; raises :class:`ConfigError` listing every
  problem, or returns ``None``.
- :func:`load_and_validate_settings` -- construct a :class:`~app.config.Settings`
  (surfacing pydantic type/range errors as :class:`ConfigError`) and then run
  :func:`validate_settings`. Returns the validated settings.
- :func:`load_settings_or_exit` -- the fail-fast process wrapper: on any
  :class:`ConfigError` it logs every offending key (never the values of
  secrets) and aborts with a non-zero ``SystemExit``; otherwise returns the
  settings. This is the entry the deploy startup wiring (task 14) calls.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from app.config import Settings

__all__ = [
    "ConfigError",
    "validate_settings",
    "load_and_validate_settings",
    "load_settings_or_exit",
    "check_runtime_secrets",
    "ensure_runtime_secrets_or_exit",
]

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration is invalid.

    Carries the complete list of human-readable problem descriptions (one per
    offending key) on :attr:`problems`, so callers can surface *all* of them at
    once rather than failing on the first.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems: list[str] = list(problems)
        super().__init__("; ".join(self.problems))


def validate_settings(settings: Settings) -> None:
    """Run conditional, cross-field validation on ``settings``.

    Raises :class:`ConfigError` (listing every offending key) when a required
    HTTP-mode value is absent; otherwise returns ``None``. Mock-mode
    implementations impose no endpoint/model requirements (Req 11.5).
    """
    problems: list[str] = []

    if settings.asr_impl == "http" and settings.asr_endpoint_url is None:
        problems.append(
            "asr_endpoint_url is required when asr_impl='http' but is not set"
        )

    if settings.llm_impl == "http":
        if settings.llm_endpoint_url is None:
            problems.append(
                "llm_endpoint_url is required when llm_impl='http' but is not set"
            )
        if settings.llm_model_name is None or not settings.llm_model_name.strip():
            problems.append(
                "llm_model_name is required when llm_impl='http' but is not set"
            )

    # Streaming ASR (live path): HTTP mode requires an endpoint URL; mock mode
    # tolerates its absence (Req 4.4, 4.5 of realtime-meeting-transcription).
    # The selector itself is constrained to {"http","mock"} by the Settings
    # ``Literal`` type, so an absent/unknown selector already fails at
    # construction (Req 4.6) -- surfaced via _problems_from_validation_error.
    if (
        settings.streaming_asr_impl == "http"
        and settings.streaming_asr_endpoint_url is None
    ):
        problems.append(
            "streaming_asr_endpoint_url is required when streaming_asr_impl='http' "
            "but is not set"
        )
    if (
        settings.streaming_asr_impl == "websocket"
        and not settings.streaming_asr_ws_url
    ):
        problems.append(
            "streaming_asr_ws_url is required when streaming_asr_impl='websocket' "
            "but is not set"
        )

    if problems:
        raise ConfigError(problems)
    return None


def _problems_from_validation_error(exc: ValidationError) -> list[str]:
    """Turn a pydantic :class:`ValidationError` into per-key problem strings.

    Each problem is prefixed with the offending field's dotted location so the
    key name (e.g. ``asr_endpoint_url``, ``asr_max_retries``) is always present
    in the message.
    """
    problems: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        problems.append(f"{loc}: {msg}" if loc else msg)
    return problems


def load_and_validate_settings(**overrides: object) -> Settings:
    """Construct :class:`~app.config.Settings` then cross-field validate it.

    Keyword overrides are passed straight to the :class:`~app.config.Settings`
    constructor (tests pass ``_env_file=None`` to ignore any local ``.env`` and
    supply explicit field values). Type/range violations raised by pydantic at
    construction are converted into a :class:`ConfigError` naming each offending
    key; a successfully constructed settings object is then passed to
    :func:`validate_settings`.

    Returns the validated :class:`~app.config.Settings` on success.
    """
    try:
        settings = Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise ConfigError(_problems_from_validation_error(exc)) from exc

    validate_settings(settings)
    return settings


def load_settings_or_exit(**overrides: object) -> Settings:
    """Load + validate configuration, aborting the process on any problem.

    On a :class:`ConfigError` this logs every offending key at ERROR (to the
    ``app.startup`` logger; secret *values* are never logged) and raises
    ``SystemExit(1)`` so a misconfigured deployment fails fast and visibly
    (Req 10.4 / 10.5). On success it returns the validated settings.
    """
    try:
        return load_and_validate_settings(**overrides)
    except ConfigError as exc:
        logger.error("Invalid configuration; aborting startup. Offending values:")
        for problem in exc.problems:
            logger.error("  - %s", problem)
        raise SystemExit(1) from exc


def check_runtime_secrets(session: object) -> None:
    """Verify stored Configuration_Secrets are decryptable with the current key.

    Reads encrypted ``runtime_config`` rows marked ``is_secret``; explicit-clear
    sentinel rows are skipped because they contain no ciphertext. If no encrypted
    rows exist this is a no-op. Otherwise ``CONFIG_SECRET_KEY`` must be present,
    a valid Fernet key, and able to decrypt every stored secret;
    otherwise a :class:`ConfigError` is raised listing the offending key(s)
    without ever logging a secret value (Req 4.6).

    Must be called **after** migrations (the ``runtime_config`` table must
    exist). Takes a live SQLAlchemy ``Session``; imports are local so importing
    this module never forces the ORM/engine into existence.
    """
    from sqlalchemy import select

    from app.models import RuntimeConfigEntry
    from app.secrets import SecretDecryptError, SecretKeyError, decrypt_secret, get_fernet

    rows = (
        session.execute(  # type: ignore[attr-defined]
            select(RuntimeConfigEntry).where(
                RuntimeConfigEntry.is_secret.is_(True),
                RuntimeConfigEntry.value != "",
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return

    try:
        get_fernet()
    except SecretKeyError as exc:
        # Absent/invalid key while encrypted secrets exist -> fail fast.
        raise ConfigError([f"CONFIG_SECRET_KEY: {exc}"]) from exc

    problems: list[str] = []
    for row in rows:
        try:
            decrypt_secret(row.value)
        except SecretDecryptError:
            problems.append(
                f"runtime_config[{row.key}]: stored secret cannot be decrypted "
                "with the current CONFIG_SECRET_KEY"
            )
    if problems:
        raise ConfigError(problems)


def ensure_runtime_secrets_or_exit(session_factory: object) -> None:
    """Run :func:`check_runtime_secrets`, aborting the process on any problem.

    Opens a short-lived session from ``session_factory`` (e.g.
    ``app.db.get_sessionmaker()``), runs the check, and on a
    :class:`ConfigError` logs every offending key (never a secret value) and
    raises ``SystemExit(1)``. Wired into the API entrypoint after migrations so
    a deployment with an undecryptable stored secret fails fast and visibly.
    """
    try:
        session = session_factory()  # type: ignore[operator]
        try:
            check_runtime_secrets(session)
        finally:
            session.close()
    except ConfigError as exc:
        logger.error(
            "Invalid configuration secrets; aborting startup. Offending values:"
        )
        for problem in exc.problems:
            logger.error("  - %s", problem)
        raise SystemExit(1) from exc
