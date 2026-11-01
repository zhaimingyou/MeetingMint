"""Health-check API router (task 12).

Implements ``GET /api/health`` -- the readiness probe from the design's
Backend_API surface (Req 12.7 / 12.8). The endpoint reports the service ready
only when **both** of its required runtime dependencies answer:

- **SQLite** -- the system of record. Probed with a trivial ``SELECT 1`` over a
  request-scoped session, which exercises opening/using a pooled connection.
- **Redis** -- the Celery broker / result backend and cache. Probed with a
  ``PING`` against ``settings.redis_url``.

Deliberately **excluded**: the ASR and LLM services. They are optional, external,
provisioned-later models reached over HTTP (and may be absent entirely in mock
mode); the readiness of the Backend_API itself must not depend on them
(Req 12.8 names "required dependencies" only). So a down ASR/LLM never flips
this endpoint to not-ready.

Within the 2-second budget (Req 12.7)
-------------------------------------
The Redis probe is given a short connect/socket timeout
(:data:`REDIS_PROBE_TIMEOUT_SECONDS`) so the whole health check returns well
inside the 2-second window even when Redis is unreachable -- a hung TCP connect
can never stall the probe past the budget. The SQLite probe is local and
effectively instantaneous.

Public endpoint
---------------
Health is intentionally **not** behind the auth dependency: an orchestrator's
liveness/readiness probe (Docker / Compose healthcheck, task 14) must reach it
without credentials. It discloses only up/down booleans for the two
dependencies -- no meeting or user data -- so leaving it unauthenticated is safe.

Testability
-----------
The two probes are module-level seams (:func:`check_database`,
:func:`check_redis`) so integration tests can monkeypatch them to simulate a
dependency being up or down (task 12.2) without needing a real Redis -- or a
forcibly-broken SQLite -- in the test environment.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings, get_settings
from app.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])

# Connect/socket timeout (seconds) for the Redis probe. Kept well under the
# 2-second health budget (Req 12.7) so an unreachable Redis fails fast as
# not-ready instead of hanging the request on a TCP connect.
REDIS_PROBE_TIMEOUT_SECONDS = 1.0


def check_database(db: OrmSession) -> bool:
    """Return ``True`` iff a trivial query against SQLite succeeds.

    Runs ``SELECT 1`` to confirm a usable connection. Any error (locked,
    missing file, driver fault) is logged and reported as down rather than
    raised, so the health endpoint always returns a structured verdict.
    """
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("health: database probe failed", exc_info=True)
        return False


def check_redis(settings: Settings) -> bool:
    """Return ``True`` iff Redis answers ``PING`` within the probe timeout.

    Connects to ``settings.redis_url`` with a bounded connect/socket timeout
    (:data:`REDIS_PROBE_TIMEOUT_SECONDS`) so the probe cannot stall the health
    check past its 2-second budget. Any error (unreachable, auth, timeout) is
    logged and reported as down.
    """
    client = None
    try:
        import redis

        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
            socket_timeout=REDIS_PROBE_TIMEOUT_SECONDS,
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("health: redis probe failed", exc_info=True)
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - close best-effort
                pass


@router.get(
    "/health",
    responses={
        status.HTTP_200_OK: {"description": "Service ready (all dependencies up)"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "A required dependency is unavailable"
        },
    },
)
def health(
    db: Annotated[OrmSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Report Backend_API readiness from its required dependencies (task 12.1).

    Probes only SQLite and Redis (ASR/LLM are deliberately excluded). Returns
    **200** with ``{"status": "ready", ...}`` when both are up (Req 12.7), or
    **503** with ``{"status": "not_ready", ...}`` naming each dependency's state
    when one is down (Req 12.8). The Redis probe is time-bounded so the response
    is produced within the 2-second budget regardless of dependency state.
    """
    database_ok = check_database(db)
    redis_ok = check_redis(settings)
    ready = database_ok and redis_ok

    payload = {
        "status": "ready" if ready else "not_ready",
        "dependencies": {
            "database": "up" if database_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
    }
    code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=payload)
