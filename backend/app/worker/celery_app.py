"""Celery application instance + operational tuning for the Job_Processor.

This module creates the single :class:`~celery.Celery` application that the
Backend_API uses to *enqueue* the meeting-processing job and that the worker
uses to *register and execute* it. It is deliberately split from the task
definition so that the API can enqueue work by **task name** without importing
the task body.

Contract with the worker pipeline (task 9.2)
--------------------------------------------
- The processing task MUST be registered under the exact name
  :data:`PROCESS_MEETING_TASK_NAME` (``"process_meeting"``) on this same
  ``celery_app``, in the module named by :data:`TASK_MODULES`
  (``app.worker.tasks``)::

      from app.worker.celery_app import celery_app

      @celery_app.task(name="process_meeting")
      def process_meeting(meeting_id: str) -> None:
          ...

  Because the app is constructed with ``include=["app.worker.tasks"]``, a worker
  booted with ``-A app.worker.celery_app:celery_app`` imports that module on
  finalize and registers the task automatically.

- The API (see ``app.api.meetings._enqueue_processing_job``) enqueues with
  ``celery_app.send_task("process_meeting", args=[meeting_id])``. Enqueue is by
  name, so it works whether or not the task function is importable in the API
  process.

Worker launch convention (task 14: Dockerfile / Compose)
--------------------------------------------------------
The Job_Processor container runs the worker as::

    celery -A app.worker.celery_app:celery_app worker \
        --loglevel=info [--concurrency=N]

``--concurrency`` is optional; when omitted the worker uses the bounded
in-process default :func:`Settings.celery_worker_concurrency` (default 2).

Timeout / retry ownership (task 9.1) -- deliberate
--------------------------------------------------
The operational tuning configured here is intentionally coarse and DOES NOT own
the per-stage timeouts or business retries (design: "Where each timeout/retry
actually lives ... do not push these onto Celery"):

- **Whole-task hard cap** -- ``task_time_limit`` is only an upper bound that
  fires if a stage hangs past its *own* timeout. The real per-stage timeouts
  live in the ffmpeg subprocess (preprocessing, task 7), the httpx request
  timeout in ``HttpAsrClient`` (task 5), and the httpx request timeout in
  ``HttpLlmClient`` (task 6).
- **No automatic retry for business failures** -- Celery's ``autoretry_for`` is
  deliberately NOT configured. A decode / ASR / LLM failure is caught inside the
  task body (task 9.2/9.3), which sets ``Job_Status = failed`` and returns
  normally, so the queue never silently re-runs a failed stage (which would,
  e.g., multiply the ``asr_max_retries`` budget). Re-processing happens ONLY via
  the explicit user-initiated resubmit (Req 7.6, task 10).
- **Infra-only redelivery** -- ``task_acks_late=True`` and
  ``task_reject_on_worker_lost=True`` give at-least-once delivery: a task that
  dies from *worker loss* (crash / OOM) is redelivered. Business exceptions do
  NOT trigger this because the task body catches them and returns success.
- **Bounded concurrency** -- ``worker_concurrency`` (default 2) plus
  ``worker_prefetch_multiplier=1`` keep ffmpeg/ASR/LLM load predictable and stop
  a worker from hoarding long tasks.

All of the tunable values above are sourced from :class:`app.config.Settings`
(``celery_task_time_limit_seconds``, ``celery_task_soft_time_limit_seconds``,
``celery_worker_concurrency``), so they are runtime-configurable via environment
without a code change.

Import safety / laziness
------------------------
Constructing a :class:`~celery.Celery` object does **not** open a broker
connection -- Celery connects lazily, only when a message is actually sent
(``send_task``) or a worker starts consuming. To keep ``import app.main`` free of
any dependency on a running broker *and* on a fully-populated environment, the
entire configuration (broker/result-backend URLs and the operational tuning) is
supplied as a **lazy default**: :func:`app.config.get_settings` is consulted
only when Celery first needs its configuration, never at import time. This means
the test suite -- which neither runs a broker nor always sets ``REDIS_URL`` --
can import the API and monkeypatch the enqueue seam without forcing a settings
load.
"""

from __future__ import annotations

from celery import Celery

from app.config import Settings, get_settings

__all__ = [
    "celery_app",
    "create_celery_app",
    "PROCESS_MEETING_TASK_NAME",
    "TASK_MODULES",
]

# Stable Celery application name (used for default queue/exchange naming).
CELERY_APP_NAME = "meeting_minutes_service"

# The exact name under which task 9.2 registers the processing task and by which
# the API enqueues it via ``send_task`` (see module docstring).
PROCESS_MEETING_TASK_NAME = "process_meeting"

# Module(s) the worker imports on finalize so the ``process_meeting`` task is
# registered when the worker boots with ``-A app.worker.celery_app:celery_app``.
# The import happens lazily at finalize (worker boot / first send), never at
# construction, so importing this module stays broker- and settings-free. Task
# 9.2 defines ``process_meeting`` in this module.
TASK_MODULES = ["app.worker.tasks"]


def _celery_config(settings: Settings | None) -> dict[str, object]:
    """Build the full Celery config from :class:`Settings` (broker + tuning).

    Redis is used as both the Celery broker and the result backend (design:
    "Redis doubles as broker + result/state cache"). The operational tuning
    (task 9.1) is derived from the ``celery_*`` Settings fields so it is
    runtime-configurable.

    When ``settings`` is ``None`` the process-wide cached
    :func:`app.config.get_settings` is read -- and, because this runs inside a
    lazy default promise, that read is deferred until Celery actually needs its
    configuration rather than happening at import time.

    Notably ABSENT: ``task_autoretry_for`` / ``autoretry_for``. Automatic task
    retry is DELIBERATELY DISABLED for business failures (decode / ASR / LLM):
    the pipeline catches them, sets ``Job_Status = failed`` and returns normally,
    so a failed stage is never silently re-run by the queue. Re-processing
    happens only through the explicit user-initiated resubmit (Req 7.6).
    """
    settings = settings if settings is not None else get_settings()

    config: dict[str, object] = {
        # --- Broker / result backend (Redis) ------------------------------
        "broker_url": settings.redis_url,
        "result_backend": settings.redis_url,
        # --- Serialization: JSON for tasks and results --------------------
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        # --- At-least-once delivery (infra-only redelivery) ---------------
        # The message is acked only after the task returns, so a worker crash
        # mid-task re-queues the meeting rather than dropping it. Business
        # failures are caught in the task and return success, so they are NOT
        # redelivered by this -- only genuine worker loss is.
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        # --- Coarse whole-task HARD cap (seconds) -- safety net only ------
        "task_time_limit": settings.celery_task_time_limit_seconds,
        # --- Bounded concurrency + no prefetch hoarding -------------------
        "worker_concurrency": settings.celery_worker_concurrency,
        "worker_prefetch_multiplier": 1,
        # --- Observability: surface a "started" state (no retry effect) ---
        "task_track_started": True,
        # --- Keep retrying the BROKER connection on worker startup --------
        # (Redis may come up slightly after the worker in Compose.) This is a
        # connection-establishment retry, unrelated to business task retries.
        "broker_connection_retry_on_startup": True,
    }

    # Optional SOFT cap: only applied when configured. Lets the task fail
    # gracefully (SoftTimeLimitExceeded) slightly before the hard kill.
    if settings.celery_task_soft_time_limit_seconds is not None:
        config["task_soft_time_limit"] = settings.celery_task_soft_time_limit_seconds

    return config


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create a Celery app fully configured from :class:`Settings`.

    The entire configuration (broker/result-backend URLs and the operational
    tuning) is registered as a **lazy default** (``app.add_defaults`` with a
    callable), so it is resolved only when Celery first needs its configuration
    -- not at construction/import time. Passing an explicit ``settings`` resolves
    against that instance instead of the cached global (used by tests).
    """
    # ``include`` modules are imported lazily at finalize (worker boot / first
    # send), so naming the task module here does NOT import it -- nor load
    # Settings -- at construction time.
    app = Celery(CELERY_APP_NAME, include=list(TASK_MODULES))
    # A callable passed to ``add_defaults`` is treated as a promise: Celery
    # evaluates it lazily on first configuration access (e.g. the first
    # ``send_task`` or worker finalize), keeping import side-effect free and
    # broker-connection free.
    app.add_defaults(lambda: _celery_config(settings))
    return app


# The process-wide Celery application. Importing this does not connect to Redis
# and does not read Settings; both happen lazily on first use.
celery_app = create_celery_app()
