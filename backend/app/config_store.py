"""Runtime configuration store + editable-field registry (admin-console, task 3).

This module owns the **persisted override layer** for runtime-editable model
configuration and the registry that defines exactly which
:class:`app.config.Settings` fields an Administrator may change at run time
(decision D4 of the design). Anything not in :data:`RUNTIME_EDITABLE` is
restart-only and cannot be written through here.

Storage model
-------------
- ``runtime_config`` holds one row per overridden key; the ``value`` is the
  stringified setting (mirroring how ``pydantic-settings`` coerces env strings),
  Fernet **ciphertext** for a set Configuration_Secret, or an empty-string
  sentinel for an explicitly cleared secret. Absence of a row means "use the
  environment default", so an empty table reproduces the current env-only
  behavior exactly (design Property 2).
- ``config_meta`` (single row ``id=1``) holds a monotonic ``version`` that is
  bumped **in the same transaction** as any write, so a reader that observes a
  new version is guaranteed to also observe the new rows (the basis for the
  no-restart propagation in :mod:`app.config_effective`).

Validation boundary
-------------------
This module performs only *structural* gatekeeping: it rejects keys that are not
in the registry. The semantic validation (type/range coercion and the
HTTP-vs-mock cross-field rules) is applied by the model-config API (task 7),
which builds a candidate effective configuration and runs the **same**
``app.startup.validate_settings`` used at startup before calling
:func:`apply_changes`.

Transaction / commit contract
-----------------------------
Mirroring :mod:`app.security.sessions`, **the caller owns the transaction.**
:func:`apply_changes` uses ``add`` / ``delete`` / ``flush`` and bumps the version
within the caller's transaction but never commits; the API route commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings
from app.models import ConfigMeta, RuntimeConfigEntry, utcnow
from app.secrets import encrypt_secret

__all__ = [
    "FieldSpec",
    "RUNTIME_EDITABLE",
    "SECRET_KEYS",
    "is_editable",
    "get_overrides",
    "get_version",
    "apply_changes",
    "UnknownConfigKeyError",
]


class UnknownConfigKeyError(KeyError):
    """Raised when a write targets a key outside :data:`RUNTIME_EDITABLE`."""


@dataclass(frozen=True)
class FieldSpec:
    """Describes one runtime-editable configuration field.

    ``kind`` drives the admin UI rendering and the task-7 range checks; the
    authoritative coercion/validation is still pydantic's when the candidate
    :class:`~app.config.Settings` is constructed. ``secret`` marks a
    Configuration_Secret (stored encrypted, never returned). ``minimum`` /
    ``maximum`` mirror the ``Field(ge=, le=)`` bounds already on ``Settings``;
    ``choices`` lists the allowed values for an ``enum`` field.
    """

    key: str
    kind: str  # one of: "enum", "str", "opt_str", "opt_url", "int", "float"
    secret: bool = False
    nullable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None


# The single source of truth for "which Settings fields are runtime-editable"
# (decision D4). Bounds mirror the Field(ge=, le=) constraints in app.config.
_SPECS: tuple[FieldSpec, ...] = (
    # --- Registration (self-service signup gate) ---
    # Whether unauthenticated visitors may POST /api/auth/register. Stored as a
    # "true"/"false" string; pydantic re-coerces it to bool when the effective
    # Settings is rebuilt. Default (no override row) is False (closed).
    FieldSpec(
        "allow_public_registration", "enum", choices=("true", "false")
    ),
    # --- ASR (batch) ---
    FieldSpec("asr_impl", "enum", choices=("http", "mock")),
    FieldSpec("asr_endpoint_url", "opt_url", nullable=True),
    FieldSpec("asr_model_name", "str"),
    FieldSpec("asr_request_timeout_seconds", "int", minimum=1, maximum=600),
    FieldSpec("asr_chunk_length_seconds", "int", minimum=30, maximum=600),
    FieldSpec("asr_max_retries", "int", minimum=0, maximum=5),
    # --- LLM ---
    FieldSpec("llm_impl", "enum", choices=("http", "mock")),
    FieldSpec("llm_endpoint_url", "opt_url", nullable=True),
    FieldSpec("llm_model_name", "opt_str", nullable=True),
    FieldSpec("llm_api_key", "opt_str", secret=True, nullable=True),
    FieldSpec("llm_request_timeout_seconds", "int", minimum=1, maximum=600),
    # --- Streaming ASR (live path) ---
    FieldSpec("streaming_asr_impl", "enum", choices=("http", "mock")),
    FieldSpec("streaming_asr_endpoint_url", "opt_url", nullable=True),
    FieldSpec("streaming_asr_timeout_seconds", "float", minimum=1, maximum=30),
    FieldSpec("streaming_send_interval_seconds", "float", minimum=0.2, maximum=5),
    FieldSpec("streaming_reconnect_interval_seconds", "float", minimum=1, maximum=30),
    # --- Live session timing ---
    FieldSpec("live_inactivity_timeout_seconds", "int", minimum=30, maximum=3600),
    FieldSpec("live_session_max_lifetime_seconds", "int", minimum=300, maximum=86400),
    FieldSpec(
        "live_session_revalidation_interval_seconds", "int", minimum=5, maximum=600
    ),
)

RUNTIME_EDITABLE: dict[str, FieldSpec] = {spec.key: spec for spec in _SPECS}
SECRET_KEYS: frozenset[str] = frozenset(
    spec.key for spec in _SPECS if spec.secret
)


def is_editable(key: str) -> bool:
    """True if ``key`` is a runtime-editable configuration field."""
    return key in RUNTIME_EDITABLE


def get_overrides(db: OrmSession) -> dict[str, str]:
    """Return all persisted overrides as ``{key: stored_value}``.

    Values are returned **as stored**: plain strings for normal fields and
    Fernet ciphertext for secret fields (decryption is the effective-config
    provider's job, and a secret value is never returned to a client). Absence
    of a key means the environment default applies.
    """
    rows = db.execute(select(RuntimeConfigEntry)).scalars().all()
    return {row.key: row.value for row in rows}


def _get_or_create_meta(db: OrmSession) -> ConfigMeta:
    """Return the singleton ``config_meta`` row, creating it (version 0) if absent.

    The Alembic migration seeds this row, but schema built via ``create_all``
    (tests) does not, so create it lazily to keep both paths working.
    """
    meta = db.get(ConfigMeta, 1)
    if meta is None:
        meta = ConfigMeta(id=1, version=0)
        db.add(meta)
        db.flush()
    return meta


def get_version(db: OrmSession) -> int:
    """Return the current monotonic configuration version (0 if never written)."""
    meta = db.get(ConfigMeta, 1)
    return int(meta.version) if meta is not None else 0


def _to_stored_str(value: object) -> str:
    """Stringify a setting value for storage (mirrors env-style string values)."""
    return value if isinstance(value, str) else str(value)


def apply_changes(
    db: OrmSession,
    changes: Mapping[str, object | None],
    *,
    actor_user_id: str | None = None,
    settings: Settings | None = None,
) -> int:
    """Persist a batch of override changes and bump the version; return it.

    For each ``key -> value`` in ``changes``:

    - The key MUST be in :data:`RUNTIME_EDITABLE`; otherwise
      :class:`UnknownConfigKeyError` is raised and **nothing** is changed
      (validate keys before calling, or rely on this guard).
    - ``value is None`` deletes a non-secret override (reset to the environment
      default). For a secret it stores an explicit-clear sentinel (an empty
      secret row), so clearing an environment-provided credential is possible.
    - Otherwise the value is stored: secret fields are Fernet-encrypted via
      :func:`app.secrets.encrypt_secret` (``is_secret=True``); normal fields are
      stringified.

    The ``config_meta`` version is incremented **once** for the whole batch,
    within the caller's transaction (no commit here). Returns the new version.

    Note: this does not range/cross-field validate -- that is the API layer's
    job (task 7) via ``app.startup.validate_settings`` on a candidate config,
    run before this call.
    """
    # Validate all keys first so a bad key changes nothing.
    unknown = [k for k in changes if k not in RUNTIME_EDITABLE]
    if unknown:
        raise UnknownConfigKeyError(
            f"not runtime-editable: {', '.join(sorted(unknown))}"
        )

    now = utcnow()
    for key, value in changes.items():
        spec = RUNTIME_EDITABLE[key]
        existing = db.get(RuntimeConfigEntry, key)
        if value is None and not spec.secret:
            # Reset to default: drop any persisted override.
            if existing is not None:
                db.delete(existing)
            continue

        stored = (
            ""
            if value is None
            else (
                encrypt_secret(_to_stored_str(value), settings)
                if spec.secret
                else _to_stored_str(value)
            )
        )
        if existing is None:
            db.add(
                RuntimeConfigEntry(
                    key=key,
                    value=stored,
                    is_secret=spec.secret,
                    updated_at=now,
                    updated_by_user_id=actor_user_id,
                )
            )
        else:
            existing.value = stored
            existing.is_secret = spec.secret
            existing.updated_at = now
            existing.updated_by_user_id = actor_user_id

    meta = _get_or_create_meta(db)
    meta.version = int(meta.version) + 1
    db.flush()
    return int(meta.version)
