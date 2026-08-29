"""LIVE: POST /jobs through the real manager, real git, real opencode serve."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.settings import Settings

SOURCE_BRANCH = "e2e-source"


def _git(*args: str, cwd: Path | None = None) -> None:
    cmd = [shutil.which("git") or "git", *args]
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def _seed_origin(origin: Path) -> None:
    origin.mkdir(parents=True)
    _git("init", cwd=origin)
    _git("config", "user.email", "e2e@opencode-manager.test", cwd=origin)
    _git("config", "user.name", "opencode-manager-e2e", cwd=origin)
    _git("config", "init.defaultBranch", "main", cwd=origin)
    (origin / "README.md").write_text("# live fixture\n", encoding="utf-8")
    _git("add", "README.md", cwd=origin)
    _git("commit", "-m", "initial", cwd=origin)
    _git("checkout", "-b", SOURCE_BRANCH, cwd=origin)
    (origin / "NOTE.txt").write_text("source\n", encoding="utf-8")
    _git("add", "NOTE.txt", cwd=origin)
    _git("commit", "-m", "branch", cwd=origin)


def _cli_free_models() -> list[str]:
    bin_path = shutil.which("opencode")
    if not bin_path:
        return []
    result = subprocess.run([bin_path, "models"], capture_output=True, text=True, timeout=90)
    return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip() and "free" in ln.lower()]


def _free_model() -> str:
    inventory = _cli_free_models()
    preferred = (
        "opencode/hy3-free",
        "opencode/mimo-v2.5-free",
        "opencode/muse-spark-1.2-contributor-free",
    )
    for item in preferred:
        if item in inventory:
            return item
    free = [m for m in inventory if "free" in m.lower() and "/" in m]
    if free:
        return free[0]
    # Last resort: live serve inventory is used by the other e2e.
    raise RuntimeError("no free model from `opencode models`: {0}".format(inventory[:20]))


@pytest.mark.live
def test_post_jobs_real_git_and_opencode(tmp_path: Path) -> None:
    if not shutil.which("opencode"):
        pytest.fail("opencode binary not found on PATH — this live test cannot skip")
    if not shutil.which("git"):
        pytest.fail("git binary not found on PATH — this live test cannot skip")

    origin = tmp_path / "origin"
    _seed_origin(origin)
    model = _free_model()
    marker = "OMGR-LIVE-{0}".format(uuid.uuid4().hex[:8].upper())
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
        hang_timeout_seconds=120.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp_path,
        opencode_bin="opencode",
    )
    settings.ensure_dirs()

    prompt = (
        "Do not use tools. Reply with exactly this token on its own line and "
        "nothing else: {0}".format(marker)
    )
    body = {
        "repo_url": origin.resolve().as_uri(),
        "PAT": "unused-for-file-url",
        "source_branch": SOURCE_BRANCH,
        "prompt": prompt,
        "model": model,
        "agent_mode": "build",
        "timeout_in_seconds": 180,
        "retry_count": 1,
        "jira_id": "LIVE-1",
        "callback_url": "http://127.0.0.1:{0}/wait".format(cb_port),
    }

    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            ack = client.post("/jobs", json=body)
            assert ack.status_code == 202, ack.text
            job_id = ack.json()["job_id"]
            assert ack.json()["jira_id"] == "LIVE-1"
            deadline = time.time() + 240
            while time.time() < deadline and not received:
                time.sleep(0.5)
            assert received, "no terminal callback from real job"
            cb = received[0]
            assert cb["job_id"] == job_id
            assert cb["jira_id"] == "LIVE-1"
            assert cb["status_code"] == 200, cb
            assert marker.lower() in (cb.get("text") or "").lower() or marker in (cb.get("text") or "")
            detail = client.get("/api/jobs/{0}".format(job_id))
            assert detail.status_code == 200
            job = detail.json()["job"]
            assert job["status"] == "success"
            assert job["session_id"].startswith("ses_")
            prompts = client.get("/api/jobs/{0}/prompts".format(job_id)).json()["prompts"]
            assert any(p["id"] == "ORIGINAL" for p in prompts)
            # Clone must be gone after job-end delete.
            leftover = list((tmp_path / "work").glob("*"))
            assert leftover == [], leftover
    finally:
        server.shutdown()
