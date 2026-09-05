"""LIVE: another process kills this job's opencode serve; OSM must start a new one.

Also covers the follow-up bug: a resumed ses_* still has the previous
job's finish=stop. Killing the serve must be serve-dead + new reply,
not 200 with the last successful output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.settings import Settings

from tests.test_live_job_e2e import SOURCE_BRANCH, _free_model, _seed_origin


def _wait_callback(received: list, *, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline and not received:
        time.sleep(0.4)
    assert received, "no terminal callback"
    return received[0]


def _job(client: TestClient, job_id: str) -> dict:
    res = client.get("/api/jobs/{0}".format(job_id))
    assert res.status_code == 200, res.text
    return res.json()["job"]


def _wait_healthy_serve(client: TestClient, job_id: str, *, timeout: float) -> tuple[int, int]:
    """Wait until OSM recorded the serve and /global/health is 200."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _job(client, job_id)
        if last.get("live") is False:
            raise AssertionError("job ended before serve was healthy: {0}".format(last))
        pid = last.get("serve_pid")
        port = last.get("serve_port")
        if pid and port:
            try:
                health = httpx.get(
                    "http://127.0.0.1:{0}/global/health".format(int(port)),
                    timeout=1.0,
                )
            except Exception:
                health = None
            if health is not None and health.status_code == 200:
                return int(pid), int(port)
        time.sleep(0.3)
    raise AssertionError("serve never became healthy: {0}".format(last))


def _wait_session_or_prompted(client: TestClient, job_id: str, *, timeout: float) -> None:
    """Prefer inner-loop (session exists) so a kill is seen on the next health poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        last = _job(client, job_id)
        if last.get("live") is False:
            return
        if last.get("session_id") or last.get("original_posted"):
            return
        time.sleep(0.3)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq {0}".format(int(pid)), "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return str(int(pid)) in out and "INFO:" not in out
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


@pytest.mark.live
def test_live_killed_serve_starts_new_serve_and_replies(tmp_path: Path) -> None:
    if not shutil.which("opencode"):
        pytest.fail("opencode binary not found on PATH — this live test cannot skip")
    if not shutil.which("git"):
        pytest.fail("git binary not found on PATH — this live test cannot skip")

    origin = tmp_path / "origin"
    _seed_origin(origin)
    model = _free_model()
    marker = "OMGR-KILL-{0}".format(uuid.uuid4().hex[:8].upper())
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cb_port = server.server_address[1]
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=1,
        callback_timeout_seconds=5.0,
        callback_retry_count=2,
        work_dir=tmp_path / "work",
        job_log_dir=tmp_path / "joblogs",
        job_store_dir=tmp_path / "jobs",
        queue_path=tmp_path / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=90.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp_path,
        opencode_bin="opencode",
        retry_backoff_seconds=0.5,
        retry_backoff_cap_seconds=2.0,
    )
    settings.ensure_dirs()
    body = {
        "repo_url": origin.resolve().as_uri(),
        "source_branch": SOURCE_BRANCH,
        "prompt": (
            "Do not use tools. Reply with exactly this token on its own line "
            "and nothing else: {0}".format(marker)
        ),
        "model": model,
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 180,
        "retry_count": 4,
        "jira_id": "LIVE-KILL-1",
        "callback_url": "http://127.0.0.1:{0}/wait".format(cb_port),
    }
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            ack = client.post("/jobs", json=body)
            assert ack.status_code == 202, ack.text
            job_id = ack.json()["job_id"]
            first_pid, _port = _wait_healthy_serve(client, job_id, timeout=90)
            _wait_session_or_prompted(client, job_id, timeout=45)
            kill_pid(first_pid)
            dead_deadline = time.time() + 10
            while time.time() < dead_deadline and _pid_alive(first_pid):
                time.sleep(0.2)
            assert not _pid_alive(first_pid), "external kill did not stop serve {0}".format(first_pid)

            seen_new_pid = False
            until = time.time() + 180
            while time.time() < until:
                job = _job(client, job_id)
                pid = job.get("serve_pid")
                if pid and int(pid) != int(first_pid):
                    seen_new_pid = True
                    break
                if job.get("live") is False:
                    break
                time.sleep(0.4)

            cb = _wait_callback(received, timeout=240)
            assert cb["job_id"] == job_id
            detail = client.get("/api/jobs/{0}".format(job_id)).json()["job"]
            kinds = [row.get("kind") for row in (detail.get("attempts") or [])]
            assert "serve-dead" in kinds, kinds
            assert seen_new_pid, "never saw a replacement serve_pid; attempts={0}".format(kinds)
            text = cb.get("text") or ""
            # A new serve must have started. If this turn also finishes, it
            # must be this marker — never a silent 200 with leftover text.
            if cb.get("status_code") == 200:
                assert marker.lower() in text.lower() or marker in text, text
                assert detail["status"] == "success"
            else:
                assert cb.get("status_code") == 500
                assert "PREVIOUS" not in text
    finally:
        server.shutdown()


@pytest.mark.live
def test_live_followup_killed_serve_does_not_reuse_prior_text(tmp_path: Path) -> None:
    if not shutil.which("opencode"):
        pytest.fail("opencode binary not found on PATH — this live test cannot skip")
    if not shutil.which("git"):
        pytest.fail("git binary not found on PATH — this live test cannot skip")

    origin = tmp_path / "origin"
    _seed_origin(origin)
    model = _free_model()
    first_mark = "OMGR-FU1-{0}".format(uuid.uuid4().hex[:8].upper())
    second_mark = "OMGR-FU2-{0}".format(uuid.uuid4().hex[:8].upper())
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            received.append(json.loads(self.rfile.read(length).decode("utf-8")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a) -> None:  # noqa: ANN002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cb = "http://127.0.0.1:{0}/wait".format(server.server_address[1])
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=1,
        callback_timeout_seconds=5.0,
        callback_retry_count=2,
        work_dir=tmp_path / "work",
        job_log_dir=tmp_path / "joblogs",
        job_store_dir=tmp_path / "jobs",
        queue_path=tmp_path / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=90.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp_path,
        opencode_bin="opencode",
        retry_backoff_seconds=0.5,
        retry_backoff_cap_seconds=2.0,
    )
    settings.ensure_dirs()
    repo = origin.resolve().as_uri()

    def post_job(client: TestClient, *, jira_id: str, prompt: str, session_id: str, retries: int) -> str:
        body = {
            "repo_url": repo,
            "source_branch": SOURCE_BRANCH,
            "prompt": prompt,
            "model": model,
            "agent_mode": "orchestrator",
            "timeout_in_seconds": 180,
            "retry_count": retries,
            "jira_id": jira_id,
            "callback_url": cb,
            "session_id": session_id,
        }
        ack = client.post("/jobs", json=body)
        assert ack.status_code == 202, ack.text
        return ack.json()["job_id"]

    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            first_id = post_job(
                client,
                jira_id="LIVE-FU",
                prompt=(
                    "Do not use tools. Reply with exactly this token on its own "
                    "line and nothing else: {0}".format(first_mark)
                ),
                session_id="",
                retries=1,
            )
            first_cb = _wait_callback(received, timeout=240)
            assert first_cb["job_id"] == first_id
            assert first_cb["status_code"] == 200, first_cb
            first_text = first_cb.get("text") or ""
            assert first_mark.lower() in first_text.lower() or first_mark in first_text
            session_id = first_cb.get("session_id") or ""
            assert session_id.startswith("ses_"), session_id
            received.clear()

            second_id = post_job(
                client,
                jira_id="LIVE-FU",
                prompt=(
                    "Do not use tools. This is a new turn. Reply with exactly "
                    "this token on its own line and nothing else: {0}".format(second_mark)
                ),
                session_id=session_id,
                retries=3,
            )
            killed_pid, _port = _wait_healthy_serve(client, second_id, timeout=90)
            _wait_session_or_prompted(client, second_id, timeout=45)
            time.sleep(5.0)
            kill_pid(killed_pid)
            dead_deadline = time.time() + 10
            while time.time() < dead_deadline and _pid_alive(killed_pid):
                time.sleep(0.2)

            second_cb = _wait_callback(received, timeout=300)
            assert second_cb["job_id"] == second_id
            text = second_cb.get("text") or ""
            detail = client.get("/api/jobs/{0}".format(second_id)).json()["job"]
            kinds = [row.get("kind") for row in (detail.get("attempts") or [])]
            assert "serve-dead" in kinds, kinds
            assert first_mark not in text
            assert detail.get("text") != first_text
            if second_cb.get("status_code") == 200:
                assert text.strip(), "success callback must be this turn, not empty"
            else:
                assert second_cb.get("status_code") == 500
    finally:
        server.shutdown()
