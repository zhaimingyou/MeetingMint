#!/usr/bin/env python3
"""End-to-end smoke test against a DEPLOYED Meeting Minutes Service.

Drives the real pipeline through the public Nginx entrypoint, exactly as a
browser would:

    login  ->  upload audio  ->  poll job status  ->  fetch transcript + minutes

This verifies the full real stack (Nginx routing, auth, upload, the Celery
worker, real SenseVoice transcription, and real DeepSeek minutes generation)
after `docker compose up -d`.

It uses ONLY the Python standard library, so it runs on the deploy host (or any
machine that can reach the service) with just `python3` -- no pip installs.

Usage
-----
    python3 scripts/e2e_smoke.py \
        --base-url http://192.0.2.10 \
        --username admin --password 'YOUR_ADMIN_PASSWORD' \
        --audio /path/to/meeting.m4a

No audio handy? Synthesize a short clip (exercises the plumbing end to end; a
pure tone yields little/no speech, so use a real recording to see real minutes):

    ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ar 16000 -ac 1 /tmp/test.wav

Exit code is 0 when the meeting reaches `completed` with minutes present, else 1.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    """Issue one HTTP request; return (status_code, body_bytes). Never raises on HTTP errors."""
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        print(f"  network error reaching {url}: {exc.reason}")
        sys.exit(1)


def _json(body: bytes):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def login(base: str, username: str, password: str) -> str:
    data = json.dumps({"username": username, "password": password}).encode("utf-8")
    status, body = _request(
        "POST", f"{base}/api/auth/login", data=data,
        headers={"Content-Type": "application/json"},
    )
    parsed = _json(body) or {}
    if status != 200 or "token" not in parsed:
        print(f"  login failed: HTTP {status} {body[:200]!r}")
        sys.exit(1)
    return parsed["token"]


def _multipart_body(field: str, filename: str, content: bytes, content_type: str) -> tuple[str, bytes]:
    """Build a minimal multipart/form-data body for a single file field."""
    boundary = "----mmsmoke" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return boundary, head + content + tail


def upload(base: str, token: str, path: str) -> str:
    with open(path, "rb") as fh:
        content = fh.read()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    boundary, body = _multipart_body("file", os.path.basename(path), content, ctype)
    status, resp = _request(
        "POST", f"{base}/api/meetings", token=token, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=600.0,
    )
    parsed = _json(resp) or {}
    if status != 201 or "id" not in parsed:
        print(f"  upload failed: HTTP {status} {resp[:300]!r}")
        sys.exit(1)
    return parsed["id"]


def poll(base: str, token: str, meeting_id: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        status, body = _request("GET", f"{base}/api/meetings/{meeting_id}/status", token=token)
        info = _json(body) or {}
        st = info.get("status")
        if st != last:
            print(f"  status: {st}")
            last = st
        if st == "completed":
            return True
        if st == "failed":
            print(f"  job FAILED: {info.get('error_description')}")
            return False
        time.sleep(3)
    print(f"  timed out after {timeout_s}s waiting for completion (last status: {last})")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end smoke test for a deployed Meeting Minutes Service.")
    ap.add_argument("--base-url", required=True, help="Public base URL, e.g. http://192.0.2.10")
    ap.add_argument("--username", required=True, help="Bootstrap admin username")
    ap.add_argument("--password", required=True, help="Bootstrap admin password")
    ap.add_argument("--audio", required=True, help="Path to an audio file (mp3/wav/m4a/webm)")
    ap.add_argument("--timeout", type=int, default=600, help="Max seconds to wait for processing (default 600)")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not os.path.isfile(args.audio):
        print(f"audio file not found: {args.audio}")
        sys.exit(1)

    print("1) login")
    token = login(base, args.username, args.password)
    print("2) upload:", args.audio)
    meeting_id = upload(base, token, args.audio)
    print("   meeting id:", meeting_id)
    print("3) poll status (preprocess -> transcribe -> generate)")
    completed = poll(base, token, meeting_id, args.timeout)

    print("4) fetch detail")
    _status, body = _request("GET", f"{base}/api/meetings/{meeting_id}", token=token)
    detail = _json(body) or {}
    segments = (detail.get("transcript") or {}).get("segments") or []
    print(f"   transcript segments: {len(segments)}")
    if segments:
        print("   first segment:", (segments[0].get("text") or "")[:60])
    minutes = detail.get("minutes") or {}
    if minutes:
        print("   summary:", (minutes.get("summary") or "")[:80])
        print("   topics:", minutes.get("topics"))
        print("   decisions:", minutes.get("decisions"))
        print("   action_items:", minutes.get("action_items"))

    ok = bool(completed and minutes)
    print("=" * 40)
    print("RESULT:", "OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
