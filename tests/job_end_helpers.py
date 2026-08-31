"""Shared fixtures for job-end kill + delete tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import clone_path_for
from opencode_manager.models import JobRecord
from opencode_manager.opencode.serve import ServeHandle
from opencode_manager.settings import Settings


def make_job(jira_id: str, **kwargs) -> JobRecord:
    data = dict(
        job_id="job_" + jira_id.lower().replace("-", "_"),
        jira_id=jira_id,
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        prompt="do work",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
        live=True,
    )
    data.update(kwargs)
    job = JobRecord(**data)
    job._pat = "secret"  # type: ignore[attr-defined]
    return job


def dest_for(settings: Settings, job: JobRecord) -> Path:
    return clone_path_for(settings.work_dir, job.jira_id)


def store_for(settings: Settings) -> JobStore:
    return JobStore(settings.job_store_dir)


def spawn_holder(dest: Path, *, seconds: int = 60) -> subprocess.Popen:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "tree.txt").write_text("alive", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-c", "import time,sys; time.sleep(int(sys.argv[1]))", str(seconds), str(dest)],
        cwd=str(dest),
    )


def wait_reaped(proc: subprocess.Popen, *, timeout: float = 3.0) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
        raise AssertionError(f"pid {proc.pid} still running after job-end cleanup")
    assert proc.poll() is not None


def seed_git_repo(root: Path, *, branch: str = "develop") -> Path:
    root.mkdir(parents=True)
    git = ["git", "-c", "user.email=t@t.test", "-c", "user.name=t"]
    subprocess.run([*git, "init"], cwd=root, check=True, capture_output=True)
    subprocess.run([*git, "checkout", "-B", "main"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run([*git, "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run([*git, "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    if branch != "main":
        subprocess.run([*git, "checkout", "-b", branch], cwd=root, check=True, capture_output=True)
        (root / "NOTE.txt").write_text("on branch\n", encoding="utf-8")
        subprocess.run([*git, "add", "NOTE.txt"], cwd=root, check=True, capture_output=True)
        subprocess.run([*git, "commit", "-m", "branch"], cwd=root, check=True, capture_output=True)
    return root


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def dummy_handle(pid: int = 424242, port: int = 19999) -> ServeHandle:
    class _Proc:
        def wait(self, timeout: Optional[float] = None) -> int:  # noqa: ARG002
            return 0

    return ServeHandle(
        pid=pid,
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        proc=_Proc(),  # type: ignore[arg-type]
        log_path=Path("/tmp/om-test-serve.log"),
    )


def user_msg(mid: str, text: str) -> dict:
    return {"id": mid, "role": "user", "parts": [{"type": "text", "text": text}]}


def assistant_msg(mid: str, text: str, *, finish: str = "stop") -> dict:
    return {
        "id": mid,
        "role": "assistant",
        "finish": finish,
        "parts": [{"type": "text", "text": text}],
    }


class ScriptedClient:
    """Stand-in for OpenCodeClient. Drive inner/outer outcomes from a script."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.session_id = "ses_scripted"
        self.closed = False
        self.aborted: list[str] = []
        self.posts: list[str] = []
        self.health_ok = True
        self._messages: list[dict] = []
        self._status: dict = {}
        self.force_create = False
        self.resume_error: Optional[Exception] = None
        self.post_error: Optional[Exception] = None
        self.health_after_posts = 0

    def close(self) -> None:
        self.closed = True

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)
        if self.script == "hang_then_success":
            self._status = {}

    def health(self) -> bool:
        if self.health_after_posts and len(self.posts) >= self.health_after_posts:
            return False
        return self.health_ok

    def resume_or_create(self, inbound, title):  # noqa: ANN001, ARG002
        if self.resume_error:
            raise self.resume_error
        if self.force_create:
            return self.session_id, True
        if inbound and inbound.startswith("ses_"):
            return inbound, False
        return self.session_id, True

    def status(self) -> dict:
        return self._status

    def session_payload(self, session_id: str) -> dict:  # noqa: ARG002
        return {}

    def list_messages(self, session_id: str) -> list[dict]:  # noqa: ARG002
        return list(self._messages)

    def post_message(self, session_id: str, text: str, model=None, agent=None) -> None:  # noqa: ANN001, ARG002
        if self.post_error:
            raise self.post_error
        self.posts.append(text)
        n = len(self.posts)
        if self.script == "success":
            self._messages = [user_msg(f"u{n}", text), assistant_msg(f"a{n}", "done work")]
        elif self.script == "asking":
            self._messages = [
                user_msg(f"u{n}", text),
                assistant_msg(f"a{n}", "Shall I continue with option A?"),
            ]
        elif self.script == "incomplete":
            self._messages = [
                user_msg(f"u{n}", text),
                assistant_msg(f"a{n}", "calling tool", finish="tool-calls"),
            ]
        elif self.script == "compact_leftover":
            self._messages = [
                user_msg(f"u{n}", text),
                assistant_msg(f"a{n}", "working"),
                {"id": f"c{n}", "role": "system", "type": "compaction", "parts": []},
            ]
        elif self.script == "hang":
            self._status = {self.session_id: {"type": "busy"}}
            self._messages = [user_msg(f"u{n}", text)]
        elif self.script == "timeout":
            self._status = {}
            self._messages = [user_msg(f"u{n}", text)]
        elif self.script == "serve-dead":
            self._messages = [user_msg(f"u{n}", text)]
            self.health_ok = False
        elif self.script == "hang_then_success":
            if n == 1:
                self._status = {self.session_id: {"type": "busy"}}
                self._messages = [user_msg("u1", text)]
            else:
                self._status = {}
                self._messages = [user_msg(f"u{n}", text), assistant_msg(f"a{n}", "recovered")]
        elif self.script == "incomplete_then_success":
            if n == 1:
                self._messages = [
                    user_msg("u1", text),
                    assistant_msg("a1", "more tools", finish="tool-calls"),
                ]
            else:
                self._messages = [user_msg(f"u{n}", text), assistant_msg(f"a{n}", "finished")]
        else:
            raise AssertionError(f"unknown script {self.script}")


def patch_opencode_loop(monkeypatch, client: ScriptedClient, handle: Optional[ServeHandle] = None):
    spawned = handle or dummy_handle()

    def start_serve(**kwargs):
        on_spawn = kwargs.get("on_spawn")
        if on_spawn:
            on_spawn(spawned)
        return spawned

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", start_serve)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", lambda *_a, **_k: client)
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry._backoff", lambda *_a, **_k: None)
    return spawned
