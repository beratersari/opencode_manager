"""Compact recap and synthetic Continue must not end the job as success."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import _inner_loop
from opencode_manager.opencode.session import OpenCodeClient, assess_idle
from opencode_manager.settings import Settings


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


def test_assess_idle_compact_summary_is_pending_not_success() -> None:
    summary = [
        {"id": "u1", "info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "do it"}]},
        {
            "id": "a_sum",
            "info": {"id": "a_sum", "role": "assistant", "finish": "stop", "summary": True},
            "parts": [{"type": "compaction", "text": "COMPACT RECAP"}],
        },
    ]
    assert assess_idle(summary) == "pending"
    assert assess_idle(summary) != "success"


def test_assess_idle_synthetic_continue_is_pending_not_leftover() -> None:
    messages = [
        {"id": "u1", "info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "do it"}]},
        {
            "id": "u_syn",
            "info": {"id": "u_syn", "role": "user"},
            "parts": [
                {
                    "type": "text",
                    "text": "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.",
                    "synthetic": True,
                    "metadata": {"compaction_continue": True},
                }
            ],
        },
    ]
    assert assess_idle(messages) == "pending"


class _CompactSummaryHandler(BaseHTTPRequestHandler):
    posted: list[str] = []

    def log_message(self, *_a) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/global/health":
            body: object = {"healthy": True}
        elif path == "/session/status":
            body = {}
        elif path == "/session/ses_csum":
            body = {"id": "ses_csum", "time": {"created": 1, "updated": 2}}
        elif path.endswith("/message"):
            body = [
                {"id": "u1", "info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "do it"}]},
                {
                    "id": "a_sum",
                    "info": {
                        "id": "a_sum",
                        "role": "assistant",
                        "finish": "stop",
                        "summary": True,
                    },
                    "parts": [{"type": "compaction", "text": "COMPACT RECAP"}],
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
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {}
            parts = payload.get("parts") if isinstance(payload, dict) else None
            if isinstance(parts, list) and parts and isinstance(parts[0], dict):
                _CompactSummaryHandler.posted.append(str(parts[0].get("text") or ""))
        self.send_response(204)
        self.end_headers()


def test_compact_summary_stop_does_not_end_job_or_nudge(tmp_settings: Settings) -> None:
    _CompactSummaryHandler.posted = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CompactSummaryHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp_settings.hang_timeout_seconds = 5.0
    client = OpenCodeClient(f"http://127.0.0.1:{server.server_address[1]}", str(tmp_settings.work_dir))
    job = JobRecord(
        job_id="job_csum",
        jira_id="CSUM-1",
        session_id="ses_csum",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        prompt="do it",
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
            deadline=time.time() + 1.2,
            should_stop=lambda: False,
            baseline_assistant_id="",
            baseline_n=1,
            baseline_compact_n=0,
        )
    finally:
        client.close()
        server.shutdown()
    assert outcome == "timeout"
    assert "COMPACT RECAP" not in (job.text or "")
    assert _CompactSummaryHandler.posted == []
