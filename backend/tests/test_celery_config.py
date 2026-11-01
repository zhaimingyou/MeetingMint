"""Unit tests for the Celery app operational config -- task 9.1.

These verify the operational tuning that the Job_Processor relies on, built on a
fresh app from explicit test Settings so no broker connection or environment is
required:

- broker/result backend resolve from ``Settings.redis_url`` (lazy, no connect);
- JSON task/result serializers + ``accept_content``;
- coarse whole-task ``task_time_limit`` == configured value (default 3600);
- ``task_acks_late`` / ``task_reject_on_worker_lost`` enabled (infra-only
  redelivery on worker loss);
- ``worker_prefetch_multiplier == 1`` and bounded ``worker_concurrency``
  (default 2);
- NO automatic retry for business failures (no ``autoretry_for`` configured);
- the ``include`` target points at the task module for task 9.2;
- the optional soft time limit is only applied when configured.

Construction must not connect to the broker (import-only safe).
"""

from __future__ import annotations

from app.config import Settings
from app.worker.celery_app import (
    PROCESS_MEETING_TASK_NAME,
    TASK_MODULES,
    create_celery_app,
)

REDIS_URL = "redis://celery-test-broker:6379/7"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "bootstrap_admin_username": "admin",
        "bootstrap_admin_password": "bootstrap-pass",
        "redis_url": REDIS_URL,
        "database_url": "sqlite://",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_broker_and_backend_from_redis_url() -> None:
    app = create_celery_app(_settings())
    assert app.conf.broker_url == REDIS_URL
    assert app.conf.result_backend == REDIS_URL


def test_json_serialization() -> None:
    app = create_celery_app(_settings())
    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert list(app.conf.accept_content) == ["json"]


def test_coarse_whole_task_time_limit_default() -> None:
    app = create_celery_app(_settings())
    # Default coarse safety cap (design example ~3600s).
    assert app.conf.task_time_limit == 3600


def test_time_limit_is_configurable() -> None:
    app = create_celery_app(_settings(celery_task_time_limit_seconds=1800))
    assert app.conf.task_time_limit == 1800


def test_acks_late_and_reject_on_worker_lost() -> None:
    app = create_celery_app(_settings())
    # Infra-only redelivery: ack after return + redeliver on worker loss.
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


def test_bounded_concurrency_and_no_prefetch_hoarding() -> None:
    app = create_celery_app(_settings())
    assert app.conf.worker_concurrency == 2  # default
    assert app.conf.worker_prefetch_multiplier == 1


def test_concurrency_is_configurable() -> None:
    app = create_celery_app(_settings(celery_worker_concurrency=4))
    assert app.conf.worker_concurrency == 4


def test_no_automatic_retry_for_business_failures() -> None:
    """Business failures must NOT trigger Celery-level auto-retry.

    The task body catches decode/ASR/LLM errors, sets Job_Status=failed and
    returns normally; re-processing is only via explicit resubmit (Req 7.6).
    Assert no global autoretry is configured.
    """
    app = create_celery_app(_settings())
    # No global autoretry-for-exceptions is configured anywhere in the conf.
    assert not app.conf.get("task_autoretry_for")
    assert not app.conf.get("autoretry_for")
    # Defensive: no stray global default retry policy enabling implicit retries.
    assert not app.conf.get("task_default_retry_delay")


def test_include_points_at_task_module_for_9_2() -> None:
    app = create_celery_app(_settings())
    # The worker autodiscovers/registers ``process_meeting`` from this module.
    assert "app.worker.tasks" in TASK_MODULES
    assert list(app.conf.include) == TASK_MODULES
    assert PROCESS_MEETING_TASK_NAME == "process_meeting"


def test_soft_time_limit_disabled_by_default() -> None:
    app = create_celery_app(_settings())
    # No soft cap unless explicitly configured.
    assert app.conf.task_soft_time_limit is None


def test_soft_time_limit_applied_when_configured() -> None:
    app = create_celery_app(_settings(celery_task_soft_time_limit_seconds=3300))
    assert app.conf.task_soft_time_limit == 3300


def test_construction_does_not_connect() -> None:
    """Building the app reads no broker; config resolves lazily on first access."""
    # Construct without touching .conf -> no settings resolution, no connect.
    app = create_celery_app(_settings())
    # Accessing .conf triggers the lazy default (still no network I/O).
    assert app.conf.broker_url == REDIS_URL
