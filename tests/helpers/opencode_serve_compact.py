"""Real process OSM starts as ``opencode serve --port N``.

After the first user POST it stays ``busy`` and ``GET /session/:id``
has ``time.compacting`` — the live OpenCode compact shape.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SESSION = "ses_compact"
MODEL = os.environ.get("OSM_SERVE_DOUBLE_MODEL") or "opencode/mimo-v2.5-free"
STATE = {"prompts": 0}


def _port() -> int:
    argv = sys.argv[1:]
    if "--port" in argv:
        return int(argv[argv.index("--port") + 1])
    return 0


def _json(handler: BaseHTTPRequestHandler, code: int, payload) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/global/health":
            _json(self, 200, {"healthy": True})
            return
        if path in {"/config/providers", "/provider"}:
            provider, _, name = MODEL.partition("/")
            _json(self, 200, {"providers": [{"id": provider, "models": {name: {}}}]})
            return
        if path == "/session/status":
            kind = "busy" if STATE["prompts"] else "idle"
            _json(self, 200, {SESSION: {"type": kind}})
            return
        if path.startswith("/session/") and path.endswith("/message"):
            _json(
                self,
                200,
                [
                    {
                        "id": "u_now",
                        "info": {"id": "u_now", "role": "user"},
                        "parts": [{"type": "text", "text": "do the work"}],
                    },
                    {
                        "id": "c_now",
                        "info": {"id": "c_now", "role": "assistant"},
                        "parts": [{"type": "compaction", "text": "Session auto-compacted"}],
                    },
                ],
            )
            return
        if path.startswith("/session/"):
            info = {"id": SESSION, "time": {"created": 1, "updated": 2}}
            if STATE["prompts"]:
                info["time"]["compacting"] = 99
            _json(self, 200, info)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        if path == "/session":
            _json(self, 200, {"id": SESSION})
            return
        if path.endswith("/prompt_async") or path.endswith("/message") or path.endswith("/abort"):
            STATE["prompts"] += 1
            _json(self, 204, {})
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    ThreadingHTTPServer(("127.0.0.1", _port()), Handler).serve_forever()


if __name__ == "__main__":
    main()
