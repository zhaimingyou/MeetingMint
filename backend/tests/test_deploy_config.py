"""Deployment configuration tests -- task 14.5.

The live "compose brings up all services" smoke tests require Docker, which is
not guaranteed in CI/dev; those are written but SKIPPED when no ``docker`` CLI
is available (mirroring how the ffmpeg-dependent tests skip). What runs
everywhere is a set of STATIC validations of the deployment artifacts authored
in task 14 -- docker-compose.yml, the reverse-proxy and SPA Nginx configs, the
Dockerfiles, .env.example, and .gitignore -- so the wiring (services, arm64
platform, shared /data volume, /api-vs-SPA routing, body-size cap, mock defaults,
no committed secrets) is verified without needing a running Docker daemon.

Mapping: 12.1 (compose brings up all services), 12.2 (linux/arm64 images),
12.3/12.4 (Nginx routing for API and SPA), 12.5/12.6 (body near limit accepted,
oversize -> 413), 11.3/11.4/11.5 (mock ASR/LLM selectable for model-less run).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
PROXY_NGINX_PATH = REPO_ROOT / "deploy" / "nginx" / "nginx.conf"
BACKEND_DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GITIGNORE = REPO_ROOT / ".gitignore"

EXPECTED_SERVICES = {"redis", "api", "worker", "frontend", "nginx"}
# Backend default MAX_UPLOAD_BYTES (Req 3.3); Nginx must allow at least this.
BACKEND_MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def _load_compose() -> dict:
    assert COMPOSE_PATH.is_file(), f"missing {COMPOSE_PATH}"
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _command_str(service: dict) -> str:
    cmd = service.get("command", "")
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


def _parse_size_to_bytes(value: str, unit: str) -> int:
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit.lower()]
    return int(value) * factor


def _defaults_to_arm64_platform(text: str) -> bool:
    """True if a compose ``platform`` / Dockerfile ``FROM`` defaults to arm64.

    Accepts either a hardcoded ``linux/arm64`` or the parameterized form that
    defaults to arm64 while allowing an override for an x86_64 host:
      * compose:    ``platform: ${DOCKER_PLATFORM:-linux/arm64}``
      * Dockerfile: ``ARG TARGET_PLATFORM=linux/arm64`` + ``FROM --platform=${TARGET_PLATFORM}``
    """
    if "linux/arm64" not in text:
        return False
    if "--platform=linux/arm64" in text or "platform: linux/arm64" in text:
        return True
    # Parameterized default -> arm64.
    if "${DOCKER_PLATFORM:-linux/arm64}" in text:
        return True
    return "ARG TARGET_PLATFORM=linux/arm64" in text and "--platform=${TARGET_PLATFORM}" in text


# --------------------------------------------------------------------------- #
# docker-compose.yml -- services, platform, volumes, commands (12.1 / 12.2)
# --------------------------------------------------------------------------- #
def test_compose_defines_all_five_services() -> None:
    compose = _load_compose()
    assert set(compose.get("services", {})) == EXPECTED_SERVICES


def test_compose_all_services_target_arm64() -> None:
    """Every service defaults to linux/arm64 images (Req 12.2).

    The platform may be pinned directly (``linux/arm64``) or via the
    ``${DOCKER_PLATFORM:-linux/arm64}`` parameter (default arm64, overridable for
    an x86_64 host); both satisfy the arm64-by-default requirement.
    """
    compose = _load_compose()
    for name, svc in compose["services"].items():
        platform = str(svc.get("platform", ""))
        assert _defaults_to_arm64_platform(platform), (
            f"{name} does not default to arm64 (platform={platform!r})"
        )


def test_compose_api_and_worker_share_one_data_volume() -> None:
    """API + worker mount the same named volume at /data (SQLite db + audio)."""
    compose = _load_compose()
    for name in ("api", "worker"):
        mounts = compose["services"][name].get("volumes", [])
        assert "appdata:/data" in mounts, f"{name} missing appdata:/data mount"
    assert {"appdata", "redis-data"}.issubset(set(compose.get("volumes", {})))


def test_compose_api_runs_migrations_then_uvicorn() -> None:
    cmd = _command_str(_load_compose()["services"]["api"])
    assert "app.entrypoint" in cmd
    assert "uvicorn app.main:app" in cmd
    assert "--dirs-only" not in cmd  # the API performs the full startup wiring


def test_compose_worker_runs_celery_without_migrating() -> None:
    cmd = _command_str(_load_compose()["services"]["worker"])
    assert "--dirs-only" in cmd  # worker does NOT migrate (avoids SQLite races)
    assert "celery -A app.worker.celery_app:celery_app worker" in cmd


def test_compose_services_depend_on_redis_health() -> None:
    compose = _load_compose()
    for name in ("api", "worker"):
        dep = compose["services"][name].get("depends_on", {})
        assert dep.get("redis", {}).get("condition") == "service_healthy"


def test_compose_nginx_publishes_port_80() -> None:
    compose = _load_compose()
    nginx = compose["services"]["nginx"]
    assert "80:80" in [str(p) for p in nginx.get("ports", [])]
    assert set(nginx.get("depends_on", [])) >= {"api", "frontend"}


# --------------------------------------------------------------------------- #
# Reverse-proxy Nginx -- routing + body cap (12.3 / 12.4 / 12.5 / 12.6)
# --------------------------------------------------------------------------- #
def test_reverse_proxy_routes_api_and_spa() -> None:
    conf = PROXY_NGINX_PATH.read_text(encoding="utf-8")
    assert "location /api/" in conf
    # /api prefix preserved (no trailing slash on proxy_pass) and sent to the API.
    assert re.search(r"location\s+/api/\s*\{[^}]*proxy_pass\s+http://api:8000;", conf, re.S)
    # Everything else proxied to the frontend SPA service.
    assert re.search(r"location\s+/\s*\{[^}]*proxy_pass\s+http://frontend:80;", conf, re.S)


def test_reverse_proxy_body_limit_at_least_backend_max() -> None:
    conf = PROXY_NGINX_PATH.read_text(encoding="utf-8")
    m = re.search(r"client_max_body_size\s+(\d+)\s*([kKmMgG]?)\s*;", conf)
    assert m, "client_max_body_size not configured"
    limit = _parse_size_to_bytes(m.group(1), m.group(2))
    assert limit >= BACKEND_MAX_UPLOAD_BYTES, "Nginx body cap is below the backend max upload"


def test_reverse_proxy_rate_limits_login() -> None:
    """The login endpoint is rate-limited to blunt password brute-forcing.

    A request-rate zone keyed by client IP must be defined and applied to an
    exact-match ``/api/auth/login`` location (so only login is throttled, not
    the rest of ``/api/*``).
    """
    conf = PROXY_NGINX_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"limit_req_zone\s+\$binary_remote_addr\s+zone=login_limit:", conf
    ), "login rate-limit zone not defined"
    assert re.search(
        r"location\s*=\s*/api/auth/login\s*\{[^}]*limit_req\s+zone=login_limit",
        conf,
        re.S,
    ), "limit_req not applied to the login location"


def test_reverse_proxy_sets_security_headers() -> None:
    """Defense-in-depth response headers are present on the reverse proxy.

    nosniff, anti-clickjacking (X-Frame-Options + CSP frame-ancestors), a
    Referrer-Policy, and a Content-Security-Policy are all sent. ``always`` so
    they reach error responses too.
    """
    conf = PROXY_NGINX_PATH.read_text(encoding="utf-8")
    assert re.search(r'add_header\s+X-Content-Type-Options\s+"?nosniff"?', conf)
    assert re.search(r"add_header\s+X-Frame-Options\s+", conf)
    assert re.search(r"add_header\s+Referrer-Policy\s+", conf)
    assert re.search(r"add_header\s+Content-Security-Policy\s+", conf)
    assert "frame-ancestors 'none'" in conf


# --------------------------------------------------------------------------- #
# Dockerfiles -- arm64 base + runtime deps (12.1 / 12.2)
# --------------------------------------------------------------------------- #
def test_backend_dockerfile_is_arm64_with_ffmpeg() -> None:
    df = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    assert _defaults_to_arm64_platform(df), "backend image does not default to linux/arm64"
    assert "ffmpeg" in df  # preprocessing + ffprobe
    # Editable install preserves alembic path resolution. The build command may
    # carry extra pip flags (e.g. --timeout/--retries for a slow/flaky PyPI), so
    # assert the editable-install intent without binding to a specific flag set.
    assert "pip install" in df and "-e ." in df


def test_frontend_dockerfile_builds_then_serves_arm64() -> None:
    df = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    assert _defaults_to_arm64_platform(df), "frontend image does not default to linux/arm64"
    # Both the build and serve stages pin --platform (literal or parameterized).
    pins = df.count("--platform=linux/arm64") + df.count("--platform=${TARGET_PLATFORM}")
    assert pins >= 2, "frontend Dockerfile must pin --platform on both stages"
    assert "npm run build" in df
    assert "nginx" in df.lower()


# --------------------------------------------------------------------------- #
# .env.example + .gitignore -- model-less defaults, no committed secrets (14.4)
# --------------------------------------------------------------------------- #
def test_env_example_has_required_keys_and_mock_defaults() -> None:
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD", "REDIS_URL"):
        assert re.search(rf"^{key}=", env, re.M), f"{key} missing from .env.example"
    # Mock defaults so the stack runs with no models (Req 11.3/11.4/11.5).
    assert re.search(r"^ASR_IMPL=mock", env, re.M)
    assert re.search(r"^LLM_IMPL=mock", env, re.M)
    # Model endpoint placeholders are present (commented) for the http switch.
    assert "ASR_ENDPOINT_URL" in env and "LLM_ENDPOINT_URL" in env and "LLM_MODEL_NAME" in env


def test_real_env_is_gitignored() -> None:
    gi = GITIGNORE.read_text(encoding="utf-8")
    assert re.search(r"^\.env$", gi, re.M), ".env must be gitignored"
    # The example template must NOT be ignored.
    assert "!.env.example" in gi
    # A local .env (holding the real secrets needed to run the stack) is
    # allowed; it must simply never be tracked by git. When a git repo is
    # present, confirm .env is actually ignored rather than asserting it is
    # absent (which would forbid a legitimate local config).
    env_path = REPO_ROOT / ".env"
    if env_path.exists() and shutil.which("git") and (REPO_ROOT / ".git").is_dir():
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, ".env exists but is not gitignored"


# --------------------------------------------------------------------------- #
# Live smoke tests -- require Docker; skipped when unavailable (12.1)
# --------------------------------------------------------------------------- #
_HAS_DOCKER = shutil.which("docker") is not None
requires_docker = pytest.mark.skipif(not _HAS_DOCKER, reason="docker CLI not available")


@requires_docker
def test_compose_config_is_valid() -> None:
    """`docker compose config` parses and resolves the compose file."""
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "services:" in result.stdout
