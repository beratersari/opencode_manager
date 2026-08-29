"""Regression: boot kills recorded pids; shutdown waits, callbacks 500, then 503."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from opencode_manager.manager import Manager
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
        "jira_id": "SD-1",
        "callback_url": "http://127.0.0.1:9/wait",
    }
    data.update(overrides)
    return data


def test_boot_kills_recorded_serve_pid(tmp_settings: Settings) -> None:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        tmp_settings.job_store_dir.mkdir(parents=True, exist_ok=True)
        from opencode_manager.dashboard.store import JobStore

        store = JobStore(tmp_settings.job_store_dir)
        store.save(
            JobRecord(
                job_id="job_orphan",
                jira_id="ORPH-1",
                status="running",
                live=True,
                serve_pid=proc.pid,
                accepted_at=utc_now(),
            )
        )
        manager = Manager(tmp_settings)
        manager.boot()
        proc.wait(timeout=3)
        assert proc.poll() is not None
        leftover = store.get("job_orphan")
        assert leftover is not None
        assert leftover.status == "error"
        assert leftover.live is False
    finally:
        if _pid_alive(proc.pid):
            proc.kill()


def test_shutdown_kills_extra_pids_and_rejects_submit(tmp_settings: Settings) -> None:
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
    hold = threading.Event()
    child = subprocess.Popen(["sleep", "30"])
    box: list[Manager] = []

    class Held:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            job.extra_pids.append(child.pid)
            box[0].store.save(job)
            deadline = time.time() + 10
            while time.time() < deadline:
                if should_stop() or hold.is_set():
                    break
                time.sleep(0.05)
            return Terminal(200, "should not finish clean")

    try:
        manager = Manager(tmp_settings, runner=Held())
        box.append(manager)
        manager.boot()
        status, env = manager.submit(_body(callback_url=f"http://127.0.0.1:{port}/wait"))
        assert status == 202
        deadline = time.time() + 2
        while time.time() < deadline:
            rec = manager.store.get(env.job_id)
            if rec and child.pid in rec.extra_pids:
                break
            time.sleep(0.05)
        assert child.pid in (manager.store.get(env.job_id).extra_pids or [])
        manager.shutdown()
        hold.set()
        child.wait(timeout=3)
        assert child.poll() is not None
        reject, _ = manager.submit(_body(jira_id="SD-2"))
        assert reject == 503
        deadline = time.time() + 2
        while time.time() < deadline and not received:
            time.sleep(0.05)
        assert len(received) == 1
        assert received[0]["status_code"] == 500
        job = manager.store.get(env.job_id)
        assert job is not None
        assert job.status == "error"
        assert not any(t.is_alive() for t in manager._threads)
    finally:
        hold.set()
        if _pid_alive(child.pid):
            child.kill()
        server.shutdown()


def test_on_done_skips_missing_queue_row(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    tmp_settings.max_concurrent_jobs = 1
    started: list[str] = []
    first_hold = threading.Event()
    second_hold = threading.Event()

    class Runner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            started.append(job.jira_id)
            if job.jira_id == "A-1":
                first_hold.wait(timeout=5)
            else:
                second_hold.wait(timeout=5)
            return Terminal(200, "ok")

    manager = Manager(tmp_settings, runner=Runner())
    manager.boot()
    try:
        a, _ = manager.submit(_body(jira_id="A-1"))
        assert a == 202
        manager.queue.enqueue({"job_id": "job_missing", "jira_id": "GHOST-1", "PAT": "x"})
        b, env_b = manager.submit(_body(jira_id="B-1"))
        assert b == 202
        first_hold.set()
        deadline = time.time() + 3
        while time.time() < deadline and "B-1" not in started:
            time.sleep(0.05)
        assert "B-1" in started
        second_hold.set()
        for t in manager._threads:
            t.join(timeout=3)
    finally:
        first_hold.set()
        second_hold.set()
        manager.stopping = True
