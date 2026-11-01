"""Model configuration API router (admin-console, task 7).

Admin-only endpoints to view, change, and test the runtime-editable model
configuration:

- ``GET  /api/admin/config``      -- the Effective_Configuration for each
  runtime-editable field (with its source) plus a read-only restart-only block;
  Configuration_Secrets render as ``configured: bool`` only, never their value.
- ``PUT  /api/admin/config``      -- validate a batch of changes with the SAME
  rules as startup, then persist atomically (all-or-nothing) + bump the version
  + write an audit entry; an omitted secret is left unchanged, an explicit
  ``null`` clears a secret even when the environment provides one.
- ``POST /api/admin/config/test`` -- probe the configured ASR/LLM services via
  the existing ``app.diagnostics`` self-check (secret-free messages).

All routes are gated by :data:`AdminUser`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import AdminUser
from app.audit import record_audit
from app.config import Settings, get_settings
from app.config_effective import effective_settings_for
from app.config_store import (
    RUNTIME_EDITABLE,
    SECRET_KEYS,
    apply_changes,
    get_overrides,
)
from app.db import get_session
from app.diagnostics import check_asr, check_llm
from app.secrets import SecretDecryptError, SecretKeyError, decrypt_secret
from app.startup import ConfigError, validate_settings

router = APIRouter(prefix="/api/admin/config", tags=["admin"])

# Curated restart-only settings shown read-only in the console (decision D4).
# Secrets/passwords are excluded; CONFIG_SECRET_KEY is reported as a bool only.
RESTART_ONLY_KEYS: tuple[str, ...] = (
    "database_url",
    "redis_url",
    "audio_storage_dir",
    "max_upload_bytes",
    "supported_formats",
    "session_ttl_minutes",
    "max_credential_length",
    "export_format",
    "celery_worker_concurrency",
    "celery_task_time_limit_seconds",
    "celery_task_soft_time_limit_seconds",
    "config_refresh_seconds",
    "bootstrap_admin_username",
)


def _json_value(value: Any) -> Any:
    """Render a setting value as JSON-safe (URL -> str, list/scalar pass through)."""
    if value is None or isinstance(value, (str, int, float, bool, list)):
        return value
    return str(value)  # e.g. AnyHttpUrl


def _project_config(base: Settings, db: OrmSession) -> dict[str, Any]:
    """Build the GET projection: editable fields (+ source) and restart-only."""
    overrides = get_overrides(db)
    effective = effective_settings_for(base, db)

    editable: list[dict[str, Any]] = []
    for key, spec in RUNTIME_EDITABLE.items():
        source = "runtime" if key in overrides else "env"
        if spec.secret:
            editable.append(
                {
                    "key": key,
                    "secret": True,
                    "configured": bool(getattr(effective, key)),
                    "source": source,
                    "editable": True,
                }
            )
        else:
            editable.append(
                {
                    "key": key,
                    "secret": False,
                    "value": _json_value(getattr(effective, key)),
                    "source": source,
                    "editable": True,
                }
            )

    restart_only: list[dict[str, Any]] = [
        {"key": key, "value": _json_value(getattr(base, key)), "editable": False}
        for key in RESTART_ONLY_KEYS
    ]
    restart_only.append(
        {
            "key": "config_secret_key",
            "secret": True,
            "configured": bool(base.config_secret_key),
            "editable": False,
        }
    )

    return {"editable": editable, "restart_only": restart_only}


def _bad_request(detail: str, errors: list[dict[str, str]]) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"detail": detail, "errors": errors}
    )


def _candidate_settings(
    base: Settings, overrides: dict[str, str], changes: dict[str, Any]
) -> Settings:
    """Build the candidate Effective_Configuration after applying ``changes``.

    Starts from the environment ``base`` and overlays the *resulting* set of
    overrides (current overrides with ``changes`` applied; a ``None`` change
    resets that field to the base default). Secret values are taken in plaintext
    (the proposed change, or the decrypted current override). Construction
    re-runs pydantic's type/range coercion, so an out-of-range value raises
    :class:`pydantic.ValidationError`.
    """
    data = base.model_dump()
    for key, spec in RUNTIME_EDITABLE.items():
        if key in changes:
            value = changes[key]
            if value is None:
                if spec.secret:
                    data[key] = None  # explicit clear overrides the environment
                continue  # non-secret reset -> keep base default
            data[key] = value
        elif key in overrides:
            raw = overrides[key]
            if spec.secret:
                if raw == "":
                    data[key] = None
                else:
                    try:
                        data[key] = decrypt_secret(raw, base)
                    except (SecretKeyError, SecretDecryptError):
                        continue  # undecryptable existing secret -> ignore for validation
            else:
                data[key] = raw
    return Settings(**data)  # type: ignore[arg-type]


@router.get("")
def get_config(
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    base: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Return the Effective_Configuration for editable + restart-only settings.

    Secrets are reported as ``configured: bool`` with no value (Req 3.1/4.2);
    each editable field is annotated with whether it comes from a persisted
    runtime override or the environment default.
    """
    return JSONResponse(status_code=200, content=_project_config(base, db))


@router.put("")
def update_config(
    changes: dict[str, Any],
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    base: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Validate then atomically persist a batch of configuration changes.

    Rejects the whole request (400) on an unknown key, an out-of-range value, or
    a failed cross-field rule, retaining the prior configuration (Req 3.4,
    Property 3). On success persists every change + bumps the version + writes an
    audit entry in one transaction, then returns the updated GET projection. An
    omitted secret is unchanged; an explicit ``null`` clears it (Req 4.4/4.5).
    """
    if not isinstance(changes, dict) or not changes:
        return _bad_request("no configuration changes provided", [])

    # (1) Structural: every key must be runtime-editable.
    unknown = [k for k in changes if k not in RUNTIME_EDITABLE]
    if unknown:
        return _bad_request(
            "unknown or restart-only configuration key(s)",
            [{"key": k, "problem": "not runtime-editable"} for k in sorted(unknown)],
        )

    overrides = get_overrides(db)

    # (2) Type/range coercion (pydantic) on the resulting effective config.
    try:
        candidate = _candidate_settings(base, overrides, changes)
    except ValidationError as exc:
        errors = [
            {"key": ".".join(str(p) for p in e.get("loc", ())), "problem": e.get("msg", "invalid")}
            for e in exc.errors()
        ]
        return _bad_request("invalid configuration value(s)", errors)

    # (3) Cross-field rules (same as startup): http impls require endpoints, etc.
    try:
        validate_settings(candidate)
    except ConfigError as exc:
        return _bad_request(
            "configuration is inconsistent",
            [{"key": "-", "problem": p} for p in exc.problems],
        )

    # (4) Persist atomically + version bump + audit, in one transaction.
    try:
        apply_changes(db, changes, actor_user_id=admin.id, settings=base)
    except SecretKeyError:
        db.rollback()
        return _bad_request(
            "cannot store a secret: CONFIG_SECRET_KEY is not configured",
            [{"key": "config_secret_key", "problem": "missing CONFIG_SECRET_KEY"}],
        )

    detail: dict[str, Any] = {"keys": sorted(changes.keys())}
    for key in changes:
        if key in SECRET_KEYS:
            detail[key] = "cleared" if changes[key] is None else "set"
    record_audit(
        db, actor=admin, action="config.update", target_type="config", detail=detail
    )
    db.commit()

    return JSONResponse(status_code=200, content=_project_config(base, db))


@router.post("/test")
def test_connectivity(
    admin: AdminUser,
    db: Annotated[OrmSession, Depends(get_session)],
    base: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Probe the configured ASR/LLM services (Req 6).

    Runs the existing ``app.diagnostics`` checks against the current
    Effective_Configuration. Each result is ``{status, message}`` with
    ``status`` in ``{ok, warn, fail, skip}`` (mock impls report ``skip`` and make
    no external call); messages are key-free.
    """
    effective = effective_settings_for(base, db)
    asr_status, asr_message = check_asr(effective)
    llm_status, llm_message = check_llm(effective)
    return JSONResponse(
        status_code=200,
        content={
            "asr": {"status": asr_status, "message": asr_message},
            "llm": {"status": llm_status, "message": llm_message},
        },
    )
