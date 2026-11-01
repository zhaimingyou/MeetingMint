"""Application entrypoint.

Constructs the FastAPI application and registers routers. The auth router
(``/api/auth/*``, task 3.4), the meetings router (``/api/meetings``, tasks 8/10/11),
and the health router (``/api/health``, task 12) are wired in here.

The app-wide handler for :class:`app.api.deps.AuthenticationError` (task 3.5) is
registered here so the auth dependency guarding every protected route renders a
no-content 401 (Req 1.1, Property 3) consistently across all current and future
routers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.admin_audit import router as admin_audit_router
from app.api.admin_config import router as admin_config_router
from app.api.admin_jobs import router as admin_jobs_router
from app.api.admin_users import router as admin_users_router
from app.api.auth import router as auth_router
from app.api.deps import (
    AuthenticationError,
    AuthorizationError,
    authentication_exception_handler,
    authorization_exception_handler,
)
from app.api.health import router as health_router
from app.api.live import router as live_router
from app.api.live import sweep_inactive_live_sessions
from app.api.meetings import router as meetings_router
from app.config import get_settings

logger = logging.getLogger(__name__)

# How often the background sweep checks for inactive live sessions. The first
# sweep runs only AFTER this delay, so short-lived test apps (which enter and
# exit the lifespan within milliseconds) cancel the task before it ever sweeps.
_SWEEP_INTERVAL_SECONDS = 5.0


async def _inactivity_sweep_loop() -> None:
    """Periodically finalize live sessions idle past the inactivity timeout (Req 7.3).

    Each iteration is fully guarded so a transient DB/config error never stops
    the loop.
    """
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            await sweep_inactive_live_sessions()
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            logger.exception("inactivity sweep iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background inactivity sweep while configuration is available.

    The sweep task is started only when :func:`app.config.get_settings` loads
    successfully (a configured deployment); otherwise it is skipped, so test
    apps that construct ``create_app()`` without a full environment are
    unaffected. The task is cancelled cleanly on shutdown.
    """
    task: asyncio.Task[None] | None = None
    try:
        get_settings()
    except Exception:
        logger.info("configuration not loadable at startup; inactivity sweep disabled")
    else:
        task = asyncio.create_task(_inactivity_sweep_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def create_app() -> FastAPI:
    """Create and return the FastAPI application instance.

    Registers the auth, live, meetings, and health routers plus the
    authentication-failure exception handler, and wires the lifespan that runs
    the background inactivity sweep for live meetings (Req 7.3).
    """
    app = FastAPI(
        title="Meeting Minutes Service",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_exception_handler(AuthenticationError, authentication_exception_handler)
    app.add_exception_handler(AuthorizationError, authorization_exception_handler)
    app.include_router(auth_router)
    app.include_router(live_router)
    app.include_router(meetings_router)
    app.include_router(health_router)
    app.include_router(admin_audit_router)
    app.include_router(admin_config_router)
    app.include_router(admin_users_router)
    app.include_router(admin_jobs_router)
    return app


app = create_app()
