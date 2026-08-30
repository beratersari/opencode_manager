from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class FakeRunner:
    def __init__(self, result: Terminal | None = None) -> None:
        self.calls: List[str] = []
        self.result = result or Terminal(200, "assistant says hi")

    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        self.calls.append(job.job_id)
        job.session_id = job.session_id or "ses_fake"
        job.text = self.result.text
        return self.result


def _body(**overrides):
    data = {
        "repo_url": "https://gitlab.example/g/r.git",
        "PAT": "not-a-real-pat",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "build",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "PROJ-1",
        "callback_url": "http://127.0.0.1:9/wait",
    }
    data.update(overrides)
    return data


def _client(settings: Settings, runner: FakeRunner | None = None) -> TestClient:
    app = create_app(settings, runner=runner or FakeRunner())
    return TestClient(app)


def test_missing_prompt_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        body = _body()
        del body["prompt"]
        res = client.post("/jobs", json=body)
        assert res.status_code == 400
        assert res.json()["status_code"] == 400
        assert res.json()["job_id"] == ""


def test_ssh_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=_body(repo_url="ssh://git@x/y.git"))
        assert res.status_code == 400


def test_unknown_agent_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=_body(agent_mode="wizard"))
        assert res.status_code == 400


def test_bad_model_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=_body(model="onlyname"))
        assert res.status_code == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("source_branch"),
        lambda b: b.pop("jira_id"),
        lambda b: b.pop("callback_url"),
        lambda b: b.pop("timeout_in_seconds"),
        lambda b: b.__setitem__("source_branch", "  "),
        lambda b: b.__setitem__("timeout_in_seconds", 0),
        lambda b: b.__setitem__("timeout_in_seconds", "nope"),
        lambda b: b.__setitem__("repo_url", "git@host:g/r.git"),
        lambda b: b.__setitem__("callback_url", "not-a-url"),
        lambda b: b.__setitem__("agent_mode", "codex"),
    ],
)
def test_inbound_error_matrix_is_400_no_job(tmp_settings: Settings, mutate) -> None:
    body = _body()
    mutate(body)
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=body)
        assert res.status_code == 400
        assert res.json()["status_code"] == 400
        assert res.json()["job_id"] == ""


def test_not_accepting_is_503(tmp_settings: Settings) -> None:
    from opencode_manager.manager import Manager

    manager = Manager(tmp_settings, runner=FakeRunner())
    status, env = manager.submit(_body())
    assert status == 503
    assert env.status_code == 503
    assert env.job_id == ""


def test_accept_and_409(tmp_settings: Settings) -> None:
    hold = threading.Event()

    class Held(FakeRunner):
        def run(self, job, *, should_stop):  # noqa: ANN001
            hold.wait(timeout=5)
            return super().run(job, should_stop=should_stop)

    with _client(tmp_settings, Held()) as client:
        first = client.post("/jobs", json=_body())
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        assert job_id.startswith("job_")
        second = client.post("/jobs", json=_body())
        assert second.status_code == 409
        assert second.json()["job_id"] == job_id
        hold.set()


def test_queue_other_ticket(tmp_settings: Settings) -> None:
    tmp_settings.max_concurrent_jobs = 1
    blocker = threading.Event()

    class SlowRunner(FakeRunner):
        def run(self, job, *, should_stop):  # noqa: ANN001
            blocker.wait(timeout=5)
            return super().run(job, should_stop=should_stop)

    runner = SlowRunner()
    with _client(tmp_settings, runner) as client:
        a = client.post("/jobs", json=_body(jira_id="A-1"))
        b = client.post("/jobs", json=_body(jira_id="B-1"))
        assert a.status_code == 202
        assert b.status_code == 202
        assert "queued" in b.json()["text"].lower()
        blocker.set()


def test_public_repo_without_pat_is_accepted(tmp_settings: Settings) -> None:
    body = _body()
    del body["PAT"]
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=body)
        assert res.status_code == 202
        assert res.json()["job_id"].startswith("job_")


def test_dashboard_writes_405(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/api/jobs", json={})
        assert res.status_code == 405


def test_pat_not_in_history_or_api(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=_body(PAT="LEAKME-PAT-VALUE"))
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        listed = client.get("/api/jobs").json()
        dumped = json.dumps(listed)
        assert "LEAKME-PAT-VALUE" not in dumped
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert "LEAKME-PAT-VALUE" not in json.dumps(detail)
        store_file = next(tmp_settings.job_store_dir.glob("*.json"))
        assert "LEAKME-PAT-VALUE" not in store_file.read_text(encoding="utf-8")


def test_one_terminal_callback(tmp_settings: Settings) -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length)
            received.append(json.loads(raw.decode("utf-8")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with _client(tmp_settings, FakeRunner(Terminal(200, "done text"))) as client:
            res = client.post(
                "/jobs",
                json=_body(callback_url=f"http://127.0.0.1:{port}/wait", jira_id="CB-1"),
            )
            assert res.status_code == 202
            for _ in range(50):
                if received:
                    break
                import time

                time.sleep(0.05)
        assert len(received) == 1
        assert received[0]["status_code"] == 200
        assert received[0]["text"] == "done text"
        assert received[0]["jira_id"] == "CB-1"
        assert received[0]["job_id"].startswith("job_")
    finally:
        server.shutdown()


def test_boot_leftover_is_error_not_409(tmp_settings: Settings) -> None:
    from opencode_manager.dashboard.store import JobStore
    from opencode_manager.models import utc_now

    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_leftover",
            jira_id="LEFT-1",
            status="running",
            live=True,
            accepted_at=utc_now(),
        )
    )
    with _client(tmp_settings) as client:
        listed = client.get("/api/jobs").json()["jobs"]
        leftover = [j for j in listed if j["job_id"] == "job_leftover"][0]
        assert leftover["status"] == "error"
        res = client.post("/jobs", json=_body(jira_id="LEFT-1"))
        assert res.status_code == 202
        assert res.json()["job_id"] != "job_leftover"
