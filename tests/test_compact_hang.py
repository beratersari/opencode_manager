"""Compact-as-busy must not fire the hang clock.

Uses a real HTTP OpenCode peer (ThreadingHTTPServer) and the real
OpenCodeClient + inner loop. No mocks of OSM.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import _inner_loop
from opencode_manager.opencode.session import OpenCodeClient
from opencode_manager.settings import Settings


class _CompactHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/global/health":
            body = {"healthy": True}
        elif path == "/session/status":
            body = {"ses_c": {"type": "busy"}}
        elif path == "/session/ses_c":
            body = {"id": "ses_c", "time": {"created": 1, "updated": 2, "compacting": 99}}
        elif path.endswith("/message"):
            body = [
                {
                    "id": "u1",
                    "info": {"id": "u1", "role": "user"},
                    "parts": [{"type": "text", "text": "do it"}],
                },
                {
                    "id": "c1",
                    "info": {"id": "c1", "role": "assistant"},
                    "parts": [{"type": "compaction", "text": "Session auto-compacted"}],
                },
            ]
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        self.send_response(204)
        self.end_headers()


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


def test_inner_loop_does_not_hang_while_session_time_compacting(tmp_settings: Settings) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CompactHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp_settings.hang_timeout_seconds = 0.4
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", "/tmp")
    job = JobRecord(
        job_id="job_compact",
        jira_id="CMP-1",
        session_id="ses_c",
        model="opencode/hy3-free",
        agent_mode="build",
        prompt="do it",
        timeout_in_seconds=8,
        retry_count=1,
    )
    try:
        started = time.time()
        outcome = _inner_loop(
            job,
            client,
            _MemStore(),
            settings=tmp_settings,
            deadline=time.time() + 1.6,
            should_stop=lambda: False,
            baseline_assistant_id="",
            baseline_n=1,
            baseline_compact_n=0,
        )
        elapsed = time.time() - started
        assert outcome != "hang", f"compact-as-busy hung after {elapsed:.2f}s"
        assert elapsed >= 1.2
        assert outcome == "timeout"
    finally:
        client.close()
        server.shutdown()
