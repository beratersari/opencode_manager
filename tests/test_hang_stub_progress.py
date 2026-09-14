"""A blank OpenCode stub assistant must not disable the hang clock."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import _inner_loop
from opencode_manager.opencode.session import OpenCodeClient
from opencode_manager.settings import Settings


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


class _StubHangHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/global/health":
            body: object = {"healthy": True}
        elif path == "/session/status":
            body = {"ses_stub": {"type": "busy"}}
        elif path == "/session/ses_stub":
            body = {"id": "ses_stub", "time": {"created": 1, "updated": 2}}
        elif path.endswith("/message"):
            body = [
                {"id": "u1", "info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "do it"}]},
                {"id": "a_stub", "info": {"id": "a_stub", "role": "assistant"}, "parts": []},
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


def test_blank_stub_assistant_still_hangs(tmp_settings: Settings) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHangHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp_settings.hang_timeout_seconds = 0.4
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", str(tmp_settings.work_dir))
    job = JobRecord(
        job_id="job_stubhang",
        jira_id="STUBH-1",
        session_id="ses_stub",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        prompt="do it",
        timeout_in_seconds=30,
        retry_count=3,
        original_posted=True,
    )
    started = time.time()
    try:
        outcome = _inner_loop(
            job,
            client,
            _MemStore(),
            settings=tmp_settings,
            deadline=time.time() + 2.0,
            should_stop=lambda: False,
            baseline_assistant_id="",
            baseline_n=1,
            baseline_compact_n=0,
        )
    finally:
        client.close()
        server.shutdown()
    assert outcome == "hang"
    assert time.time() - started < 2.0
