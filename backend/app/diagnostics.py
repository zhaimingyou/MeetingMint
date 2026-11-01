"""Real-model connectivity self-check (manual diagnostic).

Probes the *configured* external model services so an operator can confirm --
before relying on the pipeline -- that the ASR and LLM endpoints are reachable
and correctly configured, WITHOUT running a full meeting through the worker.

This is deliberately NOT part of ``GET /api/health`` (which, by design, excludes
the ASR/LLM services so the API reports ready even when no models are present).
It is an on-demand tool. Run it with the same environment the app uses, e.g.
inside the API container:

    docker compose exec api python -m app.diagnostics

What it does
------------
* **ASR** (when ``ASR_IMPL=http``): ``GET {origin}/health`` (the FunASR service's
  readiness probe; see ``deploy/asr-service/ASR-API.md``). If the reply lists
  ``models_loaded``, warn when ``ASR_MODEL_NAME`` is not among them.
* **LLM** (when ``LLM_IMPL=http``): ``GET {base}/models`` with the configured
  bearer token -- verifies reachability *and* auth in one call (a 401 here means
  a bad key). Warn when ``LLM_MODEL_NAME`` is not in the returned list.
* ``mock`` implementations are reported as skipped (no external call).

Exit code is **0** when every *configured* http service is reachable (a model
not being listed is a WARN, not a failure) and **1** when any configured service
is unreachable or rejects auth. The LLM API key is never printed.
"""

from __future__ import annotations

import sys
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings, get_settings

__all__ = ["check_asr", "check_llm", "models_url", "main"]

# Short, bounded timeout so an unreachable endpoint fails fast rather than
# hanging the self-check.
PROBE_TIMEOUT_SECONDS = 10.0

# A probe outcome: the status label plus a human-readable, key-free message.
Status = Literal["ok", "warn", "fail", "skip"]
ProbeResult = tuple[Status, str]


def _origin(url: str) -> str:
    """Return just the ``scheme://host[:port]`` origin of ``url`` (no path)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def models_url(base: str) -> str:
    """Resolve the OpenAI-compatible ``/models`` list URL from a base/endpoint.

    Mirrors the chat-completions normalization so the models probe targets the
    same server root:
      * ``https://api.deepseek.com``             -> ``https://api.deepseek.com/models``
      * ``https://api.deepseek.com/v1``          -> ``https://api.deepseek.com/v1/models``
      * ``http://host:8000/v1/chat/completions`` -> ``http://host:8000/v1/models``
    """
    u = base.rstrip("/")
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    if u.endswith("/models"):
        return u
    return f"{u}/models"


def check_asr(settings: Settings) -> ProbeResult:
    """Probe the configured ASR service; returns ``(status, message)``."""
    if settings.asr_impl != "http":
        return ("skip", f"ASR_IMPL={settings.asr_impl} (no external ASR service)")
    if settings.asr_endpoint_url is None:
        return ("fail", "ASR_IMPL=http but ASR_ENDPOINT_URL is not set")

    health = _origin(str(settings.asr_endpoint_url)) + "/health"
    try:
        with httpx.Client(timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS)) as client:
            response = client.get(health)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return ("fail", f"ASR unreachable at {health}: {exc}")

    try:
        data = response.json()
    except (httpx.HTTPError, ValueError):
        data = None
    loaded = data.get("models_loaded") if isinstance(data, dict) else None
    if isinstance(loaded, list) and settings.asr_model_name not in loaded:
        return (
            "warn",
            f"ASR reachable at {health}, but model '{settings.asr_model_name}' "
            f"is not loaded (models_loaded={loaded})",
        )
    return ("ok", f"ASR reachable at {health} (model '{settings.asr_model_name}')")


def check_llm(settings: Settings) -> ProbeResult:
    """Probe the configured LLM service (reachability + auth); returns ``(status, message)``."""
    if settings.llm_impl != "http":
        return ("skip", f"LLM_IMPL={settings.llm_impl} (no external LLM service)")
    if settings.llm_endpoint_url is None:
        return ("fail", "LLM_IMPL=http but LLM_ENDPOINT_URL is not set")

    url = models_url(str(settings.llm_endpoint_url))
    headers = (
        {"Authorization": f"Bearer {settings.llm_api_key}"}
        if settings.llm_api_key
        else None
    )
    try:
        with httpx.Client(timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS)) as client:
            response = client.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # A 401/403 surfaces here too -> almost always a bad/missing API key.
        return ("fail", f"LLM unreachable or auth rejected at {url}: {exc}")

    try:
        data = response.json()
    except (httpx.HTTPError, ValueError):
        data = None
    ids: list[str] = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        ids = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
    auth_note = "with API key" if settings.llm_api_key else "no API key"
    if settings.llm_model_name and ids and settings.llm_model_name not in ids:
        return (
            "warn",
            f"LLM reachable at {url} ({auth_note}), but model "
            f"'{settings.llm_model_name}' is not listed (available: {ids})",
        )
    return ("ok", f"LLM reachable at {url} ({auth_note}, model '{settings.llm_model_name}')")


def main(argv: list[str] | None = None) -> int:
    """Run both probes, print a report, and return an exit code (0 ok / 1 fail)."""
    settings = get_settings()
    results: dict[str, ProbeResult] = {
        "ASR": check_asr(settings),
        "LLM": check_llm(settings),
    }
    label = {"ok": " OK ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}

    print("Model connectivity self-check")
    print("=" * 40)
    failed = False
    for name, (status, message) in results.items():
        print(f"[{label[status]}] {name}: {message}")
        if status == "fail":
            failed = True
    print("=" * 40)
    print("RESULT:", "FAIL (see above)" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
