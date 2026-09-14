"""Review serve must wait for GET /session before POST /session."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opencode_manager.review_session import OpenCodeClient, OpenCodeError


def test_review_wait_directory_accepts_session_list() -> None:
    hits = {"n": 0}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

        def do_GET(self) -> None:  # noqa: N802
            hits["n"] += 1
            if self.path.startswith("/global/health"):
                raw = json.dumps({"healthy": True}).encode("utf-8")
            else:
                raw = json.dumps([]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", r"C:\osm\workspaces\1-1")
    try:
        client.wait_directory(timeout=2.0)
        assert hits["n"] >= 1
    finally:
        client.close()
        server.shutdown()


def test_review_wait_directory_times_out_when_instance_never_answers() -> None:
    client = OpenCodeClient("http://127.0.0.1:1", r"C:\osm\workspaces\1-1")
    try:
        with pytest.raises(OpenCodeError, match="serve-dead|directory instance"):
            client.wait_directory(timeout=0.4)
    finally:
        client.close()
