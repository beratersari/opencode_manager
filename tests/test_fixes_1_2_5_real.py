"""Real verification of the 1 / 2 / 5 fixes.

No unittest.mock. No FakeRunner on the paths under test.
Uses the real FastAPI app, real job store files, real git, real OS
children, a real process OSM starts as ``opencode serve``, and real HTTP.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import GitError, clone_path_for
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner

from tests.job_end_helpers import file_url, seed_git_repo, spawn_holder

STUB = Path(__file__).resolve().parent / "helpers" / "opencode_serve_compact.py"


def _body(**overrides):
    data = {
        "repo_url": "https://github.com/carltongibson/django-filter.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/mimo-v2.5-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "KEEP-2",
        "callback_url": "http://127.0.0.1:9/wait",
    }
    data.update(overrides)
    return data


def _callback_server():
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


def _wrap_stub(tmp_path: Path) -> Path:
    wrapper = tmp_path / "opencode-compact"
    wrapper.write_text(
        f"#!/bin/sh\nexec {sys.executable} {STUB} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


# --- 1. unsafe jira_id -----------------------------------------------------


def test_post_jobs_dot_does_not_wipe_sibling_clone(tmp_settings: Settings) -> None:
    keep = tmp_settings.work_dir / "KEEP-1"
    app = create_app(tmp_settings)
    holder = None
    try:
        with TestClient(app) as client:
            holder = spawn_holder(keep)
            for jira_id in (".", "..", "PROJ/99"):
                res = client.post("/jobs", json=_body(jira_id=jira_id))
                assert res.status_code == 400, (jira_id, res.text)
                assert res.json()["job_id"] == ""
                assert res.json()["status_code"] == 400
            assert list(tmp_settings.job_store_dir.glob("job_*.json")) == []
            assert holder.poll() is None
            assert keep.exists()
            assert (keep / "tree.txt").read_text(encoding="utf-8") == "alive"
            assert tmp_settings.work_dir.exists()
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.wait(timeout=2)


def test_valid_job_git_404_does_not_reap_other_ticket(tmp_settings: Settings) -> None:
    origin = seed_git_repo(tmp_settings.work_dir.parent / "origin", branch="develop")
    keep = tmp_settings.work_dir / "KEEP-1"
    server, received = _callback_server()
    app = create_app(tmp_settings)
    holder = None
    try:
        with TestClient(app) as client:
            holder = spawn_holder(keep)
            res = client.post(
                "/jobs",
                json=_body(
                    repo_url=file_url(origin),
                    source_branch="no-such-branch",
                    jira_id="NEW-1",
                    callback_url=f"http://127.0.0.1:{server.server_address[1]}/w",
                ),
            )
            assert res.status_code == 202
            job_id = res.json()["job_id"]
            deadline = time.time() + 20
            while time.time() < deadline and not received:
                time.sleep(0.1)
            assert received, "worker never sent the terminal callback"
            assert received[0]["status_code"] == 404
            job = client.get(f"/api/jobs/{job_id}")
            assert job.status_code == 200
            assert job.json()["job"]["status"] == "not_found"
            assert holder.poll() is None
            assert keep.exists()
            assert not (tmp_settings.work_dir / "NEW-1").exists()
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.wait(timeout=2)
        server.shutdown()


def test_runner_refuses_dot_jira_and_leaves_sibling(tmp_settings: Settings) -> None:
    keep = tmp_settings.work_dir / "KEEP-1"
    holder = spawn_holder(keep)
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord.model_construct(
        job_id="job_dot",
        jira_id=".",
        repo_url="https://example/r.git",
        source_branch="develop",
        prompt="x",
        model="opencode/mimo-v2.5-free",
        agent_mode="build",
        status="running",
        live=True,
    )

    try:
        try:
            OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
        except GitError:
            pass
        assert holder.poll() is None
        assert keep.exists()
        assert tmp_settings.work_dir.exists()
        try:
            clone_path_for(tmp_settings.work_dir, ".")
        except GitError:
            pass
        else:
            raise AssertionError("clone_path_for('.') must raise")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=2)


# --- 2. compact is not hang ------------------------------------------------


def test_full_job_stays_up_while_serve_reports_compacting(tmp_settings: Settings, tmp_path: Path) -> None:
    origin = seed_git_repo(tmp_path / "origin", branch="develop")
    tmp_settings.opencode_bin = str(_wrap_stub(tmp_path))
    tmp_settings.hang_timeout_seconds = 0.4
    tmp_settings.git_clone_timeout_seconds = 20.0
    os.environ["OSM_SERVE_DOUBLE_MODEL"] = "opencode/mimo-v2.5-free"
    server, received = _callback_server()
    app = create_app(tmp_settings)
    try:
        with TestClient(app) as client:
            res = client.post(
                "/jobs",
                json=_body(
                    repo_url=file_url(origin),
                    source_branch="develop",
                    jira_id="CMP-1",
                    model="opencode/mimo-v2.5-free",
                    timeout_in_seconds=4,
                    retry_count=1,
                    callback_url=f"http://127.0.0.1:{server.server_address[1]}/w",
                    prompt="do the work",
                ),
            )
            assert res.status_code == 202
            job_id = res.json()["job_id"]
            time.sleep(1.2)
            mid = client.get(f"/api/jobs/{job_id}").json()["job"]
            assert mid["status"] == "running", (
                f"compact-as-busy ended early: {mid['status']} {mid.get('error_message')}"
            )
            deadline = time.time() + 15
            while time.time() < deadline and not received:
                time.sleep(0.1)
            assert received, "job never sent a terminal callback"
            text = received[0].get("text") or ""
            assert received[0]["status_code"] != 200
            assert "hang" not in text.lower()
    finally:
        os.environ.pop("OSM_SERVE_DOUBLE_MODEL", None)
        server.shutdown()


# --- 5. list filter is the filtered set (server) + UI race is in vitest ---


def test_api_filter_error_is_not_the_all_page(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_ok",
            jira_id="OK-1",
            status="success",
            live=False,
            accepted_at=utc_now(),
        )
    )
    store.save(
        JobRecord(
            job_id="job_err",
            jira_id="ERR-1",
            status="error",
            live=False,
            accepted_at=utc_now(),
        )
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        all_page = client.get("/api/jobs", params={"filter": "all"})
        err_page = client.get("/api/jobs", params={"filter": "error"})
        assert all_page.status_code == 200
        assert {j["jira_id"] for j in all_page.json()["jobs"]} == {"OK-1", "ERR-1"}
        assert err_page.json()["total"] == 1
        assert err_page.json()["jobs"][0]["jira_id"] == "ERR-1"
        assert err_page.json()["filter"] == "error"
