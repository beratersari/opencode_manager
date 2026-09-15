"""Daily-usage probes against the real app, real git, and a real HTTP listener.

No mocks. Failures here are the only critical claims this review will list.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import public_git_url
from opencode_manager.settings import Settings


def _git(*args: str, cwd: Path | None = None) -> None:
    exe = shutil.which("git") or "git"
    result = subprocess.run([exe, *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def _local_repo(tmp: Path) -> Path:
    src = tmp / "origin"
    src.mkdir()
    _git("init", cwd=src)
    _git("config", "user.email", "daily@opencode-manager.test", cwd=src)
    _git("config", "user.name", "daily", cwd=src)
    (src / "README.md").write_text("daily-usage\n", encoding="utf-8")
    _git("add", "README.md", cwd=src)
    _git("commit", "-m", "seed", cwd=src)
    return src


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=2,
        callback_timeout_seconds=2.0,
        callback_retry_count=1,
        data_dir=tmp / "data",
        work_dir=tmp / "work",
        job_log_dir=tmp / "joblogs",
        job_store_dir=tmp / "jobs",
        queue_path=tmp / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=15.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp,
    )
    settings.ensure_dirs()
    dist = tmp / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA-INDEX</html>", encoding="utf-8")
    return settings


def _body(**overrides):
    data = {
        "repo_url": "https://example.invalid/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 20,
        "retry_count": 1,
        "jira_id": "DAILY-1",
    }
    data.update(overrides)
    return data


def _wait_terminal(client: TestClient, job_id: str, *, tries: int = 80):
    last = None
    for _ in range(tries):
        last = client.get(f"/jobs/{job_id}")
        if last.status_code == 200 and last.json().get("live") is False:
            return last
        time.sleep(0.25)
    return last


def test_poller_jobs_path_is_json_not_spa(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        res = client.get("/jobs/job_doesnotexist")
        assert res.status_code == 404
        assert res.headers.get("content-type", "").startswith("application/json")
        body = res.json()
        assert "status_code" in body
        assert "SPA-INDEX" not in res.text
        spa = client.get("/jobs-ui")
        assert spa.status_code == 200
        assert "SPA-INDEX" in spa.text


def test_inbound_userinfo_is_not_on_disk_or_public_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    dirty = "https://oauth2:super-secret-pat@127.0.0.1/unused.git"
    app = create_app(settings)
    with TestClient(app) as client:
        res = client.post(
            "/jobs",
            json=_body(
                repo_url=dirty,
                jira_id="DAILY-AUTH",
                session_id="-1",
            ),
        )
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        done = _wait_terminal(client, job_id)
        assert done is not None
        detail = client.get(f"/api/jobs/{job_id}").json()["job"]
        assert "super-secret-pat" not in json.dumps(detail)
        assert "oauth2:" not in (detail.get("repo_url") or "")
        assert "callback_url" not in detail
        chat = client.get(f"/api/jobs/{job_id}/chat").json()
        assert chat["messages"] == [] or isinstance(chat["messages"], list)
        on_disk = list((tmp_path / "jobs").glob("*.json"))
        blob = "\n".join(p.read_text(encoding="utf-8") for p in on_disk)
        assert "super-secret-pat" not in blob
        assert public_git_url(dirty) == "https://127.0.0.1/unused.git"


def test_real_clone_is_gone_when_job_ends(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = _local_repo(tmp_path)
    url = repo.resolve().as_uri()
    dest = Path(settings.work_dir) / "DAILY-CLONE"
    app = create_app(settings)
    with TestClient(app) as client:
        res = client.post(
            "/jobs",
            json=_body(repo_url=url, jira_id="DAILY-CLONE", session_id="-1"),
        )
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        done = _wait_terminal(client, job_id)
        assert done is not None
        assert done.json().get("live") is False
        assert not dest.exists()
        poll = done.json()
        assert poll["status_code"] in {200, 500, 504}
        assert poll["job_id"] == job_id


def test_same_ticket_second_post_409_while_first_live(tmp_path: Path) -> None:
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            release.wait(timeout=30)
            self.send_response(404)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/repo.git"
    settings = _settings(tmp_path)
    settings.git_clone_timeout_seconds = 45.0
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            first = client.post(
                "/jobs",
                json=_body(repo_url=url, jira_id="DAILY-DUP", timeout_in_seconds=120),
            )
            assert first.status_code == 202
            for _ in range(40):
                mid = client.get(f"/jobs/{first.json()['job_id']}")
                if mid.status_code == 202:
                    break
                time.sleep(0.05)
            second = client.post(
                "/jobs",
                json=_body(repo_url=url, jira_id="DAILY-DUP", timeout_in_seconds=120),
            )
            assert second.status_code == 409
            assert second.json()["status_code"] == 409
            assert second.json()["job_id"] == first.json()["job_id"]
            release.set()
            _wait_terminal(client, first.json()["job_id"], tries=80)
    finally:
        release.set()
        httpd.shutdown()


def test_chat_placeholder_session_does_not_hit_opencode(tmp_path: Path) -> None:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        settings = _settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            res = client.post(
                "/jobs",
                json=_body(
                    repo_url=_local_repo(tmp_path).resolve().as_uri(),
                    jira_id="DAILY-CHAT",
                    session_id="-1",
                ),
            )
            job_id = res.json()["job_id"]
            done = _wait_terminal(client, job_id)
            assert done is not None
            store = JobStore(tmp_path / "jobs")
            job = store.get(job_id)
            assert job is not None
            job.session_id = "-1"
            job.serve_base_url = f"http://{host}:{port}"
            job.live = True
            job.clone_path = str(tmp_path / "work" / "DAILY-CHAT")
            store.save(job)
            chat = client.get(f"/api/jobs/{job_id}/chat")
            assert chat.status_code == 200
            assert chat.json().get("messages") == []
        assert hits == []
    finally:
        httpd.shutdown()
