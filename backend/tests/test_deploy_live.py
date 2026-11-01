"""Deployment-config checks for the live path (task 11).

Static validations (no Docker) that the Nginx reverse proxy forwards the
WebSocket upgrade for the live Streaming_Connection and that the API service is
pinned to a single process / single replica (design FIX C).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_NGINX_PATH = REPO_ROOT / "deploy" / "nginx" / "nginx.conf"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


def test_nginx_forwards_websocket_upgrade_on_api() -> None:
    conf = PROXY_NGINX_PATH.read_text(encoding="utf-8")
    # The http-scope connection-upgrade map.
    assert re.search(
        r"map\s+\$http_upgrade\s+\$connection_upgrade\s*\{", conf
    ), "missing connection_upgrade map"
    # Inside the /api/ location: Upgrade + Connection headers forwarded.
    api_block = re.search(r"location\s+/api/\s*\{(.*?)\}", conf, re.S)
    assert api_block, "no /api/ location block"
    body = api_block.group(1)
    assert "proxy_set_header Upgrade $http_upgrade;" in body
    assert "proxy_set_header Connection $connection_upgrade;" in body
    # Idle timeout raised well above the live inactivity timeout (default 300s).
    m = re.search(r"proxy_read_timeout\s+(\d+)s;", body)
    assert m and int(m.group(1)) >= 600, "proxy_read_timeout too low for live sockets"


def test_api_runs_single_process_single_replica() -> None:
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    api = compose["services"]["api"]
    cmd = api.get("command", "")
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    # The live path requires a single uvicorn worker (no multi-worker server).
    assert "--workers" not in cmd_str, "API must not run multiple uvicorn workers"
    assert "gunicorn" not in cmd_str, "API must not run multi-worker gunicorn"
    # No compose-level scaling of the API service.
    assert "replicas" not in str(api.get("deploy", {})), "API must not be scaled"
    # The constraint is documented (so a future maintainer does not silently
    # break it by adding workers/replicas).
    assert "single process" in raw or "single replica" in raw
