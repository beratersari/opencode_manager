#!/usr/bin/env python3
"""Test client: POST /jobs to OSM and listen for the terminal callback."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
REPLIES = ROOT / "replies.jsonl"
PAGE = ROOT / "index.html"

_lock = threading.Lock()
_events: list[dict[str, Any]] = []
_osm = "http://127.0.0.1:8080"
_public_base = "http://127.0.0.1:8090"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _add(event: dict[str, Any]) -> None:
    event.setdefault("received_at", _now())
    with _lock:
        _events.insert(0, event)
        del _events[200:]
    with REPLIES.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _send(handler: BaseHTTPRequestHandler, code: int, body: Any, *, html: bool = False) -> None:
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _post_osm(payload: dict[str, Any]) -> tuple[int, Any]:
    raw = json.dumps(payload).encode("utf-8")
    req = Request(
        _osm.rstrip("/") + "/jobs",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            parsed = json.loads(text) if text else {"text": text}
        except json.JSONDecodeError:
            parsed = {"text": text, "status_code": exc.code}
        return exc.code, parsed
    except URLError as exc:
        return 0, {"text": f"could not reach OSM at {_osm}: {exc.reason}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            _send(self, 200, PAGE.read_bytes(), html=True)
            return
        if path == "/inbox":
            with _lock:
                items = list(_events)
            _send(self, 200, {"events": items, "osm": _osm, "callback_url": _public_base + "/callback"})
            return
        if path == "/health":
            _send(self, 200, {"ok": True})
            return
        _send(self, 404, {"text": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/send":
            body = _json(self)
            callback = _public_base.rstrip("/") + "/callback"
            payload = {
                "repo_url": (body.get("repo_url") or "").strip(),
                "PAT": (body.get("PAT") or "").strip(),
                "source_branch": (body.get("source_branch") or "").strip(),
                "prompt": body.get("prompt") or "",
                "model": (body.get("model") or "").strip(),
                "agent_mode": (body.get("agent_mode") or "").strip(),
                "timeout_in_seconds": int(body.get("timeout_in_seconds") or 1800),
                "retry_count": int(body.get("retry_count") or 1),
                "jira_id": (body.get("jira_id") or f"TEST-{uuid.uuid4().hex[:6].upper()}").strip(),
                "callback_url": callback,
            }
            session_id = (body.get("session_id") or "").strip()
            if session_id:
                payload["session_id"] = session_id
            status, ack = _post_osm(payload)
            _add(
                {
                    "kind": "ack",
                    "http_status": status,
                    "callback_url": callback,
                    "request": {k: v for k, v in payload.items() if k != "PAT"},
                    "body": ack,
                }
            )
            _send(self, 200, {"http_status": status, "body": ack})
            return
        if path == "/callback" or path.startswith("/callback/"):
            body = _json(self)
            _add({"kind": "callback", "http_status": 200, "body": body})
            _send(self, 200, {"ok": True})
            return
        if path == "/clear":
            with _lock:
                _events.clear()
            _send(self, 200, {"ok": True})
            return
        _send(self, 404, {"text": "not found"})


def main() -> None:
    global _osm, _public_base
    parser = argparse.ArgumentParser(description="OSM job tester (send + listen)")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--osm", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--public-base",
        default="",
        help="URL OSM should POST callbacks to (default http://LISTEN:PORT)",
    )
    args = parser.parse_args()
    _osm = args.osm.rstrip("/")
    _public_base = (args.public_base or f"http://{args.listen}:{args.port}").rstrip("/")
    REPLIES.touch(exist_ok=True)
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    print(f"tester UI      {_public_base}", flush=True)
    print(f"callback URL   {_public_base}/callback", flush=True)
    print(f"OSM target     {_osm}/jobs", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
