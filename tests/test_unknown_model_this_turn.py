"""Unknown-model detect must ignore the previous job on a resumed ses_*."""

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


class _PriorModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/global/health":
            body: object = {"healthy": True}
        elif path == "/session/status":
            body = {}
        elif path == "/session/ses_prior":
            body = {"id": "ses_prior", "time": {"created": 1, "updated": 2}}
        elif path.endswith("/message"):
            body = [
                {
                    "id": "a_old",
                    "info": {
                        "id": "a_old",
                        "role": "assistant",
                        "finish": "stop",
                        "error": {"name": "ProviderModelNotFoundError"},
                    },
                    "parts": [{"type": "text", "text": "model not found: old/model"}],
                },
                {
                    "id": "u_now",
                    "info": {"id": "u_now", "role": "user"},
                    "parts": [{"type": "text", "text": "do the work"}],
                },
                {
                    "id": "a_now",
                    "info": {"id": "a_now", "role": "assistant", "finish": "stop"},
                    "parts": [{"type": "text", "text": "THIS TURN ONLY"}],
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


def test_unknown_model_scan_ignores_prior_job_history(tmp_settings: Settings) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PriorModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", str(tmp_settings.work_dir))
    job = JobRecord(
        job_id="job_prior",
        jira_id="PRIOR-1",
        session_id="ses_prior",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        prompt="do the work",
        timeout_in_seconds=30,
        retry_count=1,
        original_posted=True,
    )
    try:
        outcome = _inner_loop(
            job,
            client,
            _MemStore(),
            settings=tmp_settings,
            deadline=time.time() + 3.0,
            should_stop=lambda: False,
            baseline_assistant_id="a_old",
            baseline_n=1,
            baseline_compact_n=0,
        )
    finally:
        client.close()
        server.shutdown()
    assert outcome == "success"
    assert job.text == "THIS TURN ONLY"
