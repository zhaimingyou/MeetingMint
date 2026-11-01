"""Effective configuration provider (admin-console, task 4).

Computes the **Effective_Configuration**: the environment-provided
:class:`app.config.Settings` overlaid with the persisted ``runtime_config``
overrides for the runtime-editable fields (decision D4). This is what the
model-client factories and the worker pipeline read so an Administrator's change
takes effect **without a restart** (Req 5), while a deployment with no overrides
behaves exactly like the current env-only configuration (Property 2).

Two entry points, matching the two ways configuration enters the codebase:

- :func:`effective_settings` / :class:`ConfigProvider` -- for the **worker** and
  any caller whose base is the process-wide :func:`app.config.get_settings`.
  The provider caches the built :class:`Settings` keyed by the persisted config
  ``version`` and only re-reads the DB when a bounded TTL
  (``config_refresh_seconds``) has elapsed, then rebuilds only if the version
  changed -- giving cross-process, no-restart propagation cheaply.
- :func:`effective_settings_for` -- for **HTTP/WS routes** that already hold a
  base :class:`Settings` (injected via ``Depends(get_settings)``) and a request
  session. It overlays the persisted overrides onto *that* base, so a test that
  overrides the ``get_settings`` dependency keeps full control (an empty override
  table yields the injected base unchanged).

Robustness: a persisted override that fails to decrypt or to re-validate (e.g. a
hand-edited DB row) is skipped/falls back to the base rather than taking the
service down -- the model-config API validates every change before persisting,
so this only guards against out-of-band tampering.
"""

from __future__ import annotations

import logging
import threading
import time

from pydantic import ValidationError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.config_store import RUNTIME_EDITABLE, get_overrides, get_version
from app.secrets import SecretDecryptError, SecretKeyError, decrypt_secret

logger = logging.getLogger(__name__)

__all__ = [
    "overlay_settings",
    "effective_settings_for",
    "ConfigProvider",
    "get_config_provider",
    "effective_settings",
    "reset_config_provider",
]


def overlay_settings(base: Settings, overrides: dict[str, str]) -> Settings:
    """Return ``base`` with runtime-editable ``overrides`` applied (pure).

    Each override is a stored string for a registry field (Fernet ciphertext for
    a secret). Non-registry keys are ignored. Secrets are decrypted using
    ``base`` (which carries ``config_secret_key``). The overlaid values are fed
    back through :class:`Settings` construction so pydantic re-coerces/validates
    them exactly as it does for environment strings.

    With no applicable overrides this returns ``base`` itself (so
    ``overlay_settings(base, {})`` is identically ``base`` -- Property 2). If the
    overlaid configuration fails to construct (a tampered/out-of-range stored
    value), this logs a warning and falls back to ``base`` so the service stays
    up.
    """
    if not overrides:
        return base

    data = base.model_dump()
    changed = False
    for key, raw in overrides.items():
        spec = RUNTIME_EDITABLE.get(key)
        if spec is None:
            continue  # stale/unknown key -- ignore
        if spec.secret:
            if raw == "":
                value: object = None
            else:
                try:
                    value = decrypt_secret(raw, base)
                except (SecretKeyError, SecretDecryptError):
                    logger.warning(
                        "skipping undecryptable runtime override for %r", key
                    )
                    continue
        else:
            value = raw
        data[key] = value
        changed = True

    if not changed:
        return base

    try:
        return Settings(**data)  # type: ignore[arg-type]
    except ValidationError:
        logger.warning(
            "runtime configuration override failed validation; "
            "falling back to base configuration"
        )
        return base


def effective_settings_for(base: Settings, db: OrmSession) -> Settings:
    """Overlay the persisted overrides (read from ``db``) onto an injected ``base``.

    For HTTP/WS routes that already hold a request-scoped base ``Settings`` and a
    session. No process-wide caching is used, so the route always reflects the
    latest persisted overrides; the read is a single small query.
    """
    return overlay_settings(base, get_overrides(db))


class ConfigProvider:
    """Version-stamped, TTL-bounded cache of the Effective_Configuration.

    Used where the base is the process-wide :func:`app.config.get_settings`
    (the worker and global callers). Thread-safe; each Celery prefork child and
    the API process hold their own instance.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: Settings | None = None
        self._version: int | None = None
        self._checked_monotonic: float = 0.0

    def effective(
        self, session_factory: sessionmaker[OrmSession] | None = None
    ) -> Settings:
        """Return the current Effective_Configuration (base = ``get_settings()``).

        Serves the cached value while within ``config_refresh_seconds`` of the
        last DB check; otherwise re-reads the persisted version and rebuilds only
        when it changed.
        """
        base = get_settings()
        ttl = float(base.config_refresh_seconds)
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and (now - self._checked_monotonic) < ttl:
                return self._cached

            from app.db import get_sessionmaker

            factory = session_factory or get_sessionmaker()
            session = factory()
            try:
                version = get_version(session)
                if self._cached is not None and version == self._version:
                    self._checked_monotonic = now
                    return self._cached
                overrides = get_overrides(session)
            finally:
                session.close()

            self._cached = overlay_settings(base, overrides)
            self._version = version
            self._checked_monotonic = now
            return self._cached

    def reset(self) -> None:
        """Clear the cache (tests / after a known config change in-process)."""
        with self._lock:
            self._cached = None
            self._version = None
            self._checked_monotonic = 0.0


_provider = ConfigProvider()


def get_config_provider() -> ConfigProvider:
    """Return the process-wide :class:`ConfigProvider` singleton."""
    return _provider


def effective_settings(
    session_factory: sessionmaker[OrmSession] | None = None,
) -> Settings:
    """Convenience accessor for the process-wide provider's effective config."""
    return _provider.effective(session_factory)


def reset_config_provider() -> None:
    """Reset the process-wide provider cache (test isolation)."""
    _provider.reset()
