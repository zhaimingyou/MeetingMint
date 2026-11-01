"""Smoke test verifying the project scaffold imports cleanly.

Confirms the `app` package is importable and exposes a FastAPI application
instance. Functional behavior is covered by tests added in later tasks.
"""

from __future__ import annotations

from fastapi import FastAPI


def test_app_package_imports() -> None:
    import app

    assert app.__version__


def test_fastapi_app_instance_exists() -> None:
    from app.main import app

    assert isinstance(app, FastAPI)


def test_create_app_factory() -> None:
    from app.main import create_app

    instance = create_app()
    assert isinstance(instance, FastAPI)
    assert instance.title == "Meeting Minutes Service"
