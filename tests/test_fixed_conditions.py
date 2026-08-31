"""Regression locks for conditions that were already fixed.

Each test name is a fixed condition. Do not weaken the asserts.
See agents/fixed-conditions.md.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import (
    GitError,
    _run_git,
    clone_identity,
    clone_path_for,
    ls_remote_has_branch,
)
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.opencode.serve import ServeHandle, start_serve, wait_health
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner


def _job(jira_id: str, **kwargs) -> JobRecord:
    data = dict(
        job_id="job_" + jira_id.lower().replace("-", ""),
        jira_id=jira_id,
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        prompt="do work",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
    )
    data.update(kwargs)
    job = JobRecord(**data)
    job._pat = "secret"  # type: ignore[attr-defined]
    return job


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_git_error_after_partial_clone_deletes_dest(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-GITERR")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def clone_fail(*_a, **_k) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_text("half", encoding="utf-8")
        raise GitError("clone exploded after dest created")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_fail)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert not dest.exists()


def test_missing_branch_404_deletes_leftover_dest(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-404", source_branch="nope")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True)
    (dest / "stale").write_text("x", encoding="utf-8")
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 404
    assert not dest.exists()


def test_opencode_jobfailed_still_deletes_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-JF")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def clone_ok(*_a, **_k) -> None:
        dest.mkdir(parents=True)
        (dest / "ok").write_text("tree", encoding="utf-8")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(500, "still asking")),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert not dest.exists()


def test_opencode_unexpected_exception_still_deletes_clone(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-CRASH")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def clone_ok(*_a, **_k) -> None:
        dest.mkdir(parents=True)
        (dest / "ok").write_text("tree", encoding="utf-8")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "boom" in terminal.text
    assert not dest.exists()


def test_leftover_clone_gone_before_git_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-PRE")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True)
    (dest / "stale").write_text("old", encoding="utf-8")
    seen: dict[str, bool] = {}

    def clone_ok(*_a, **_k) -> None:
        seen["existed"] = dest.exists()
        dest.mkdir(parents=True)
        (dest / "fresh").write_text("new", encoding="utf-8")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: SimpleNamespace(status_code=200, text="done"),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert seen["existed"] is False
    assert not dest.exists()
    assert job.clone_path == str(dest)


def test_no_clone_if_leftover_cannot_be_removed(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-STUCK")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True)
    cloned = {"n": 0}

    def stay(*_a, **_k) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        return False

    monkeypatch.setattr("opencode_manager.cleanup.end.hard_delete", stay)
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo", lambda *_a, **_k: cloned.__setitem__("n", cloned["n"] + 1)
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "could not remove leftover clone" in terminal.text
    assert cloned["n"] == 0


def test_clone_path_is_ticket_only() -> None:
    work = Path("/var/lib/osm/.temp")
    assert clone_path_for(work, "PROJ-99") == work / "PROJ-99"
    assert clone_identity("PROJ-99") == "PROJ-99"
    same_ticket = clone_path_for(work, "PROJ-99")
    assert same_ticket == clone_path_for(work, "PROJ-99")
    assert clone_path_for(work, "PROJ-1") != clone_path_for(work, "PROJ-2")


def test_clone_path_ignores_repo_and_branch() -> None:
    """Folder is the ticket. Repo/branch are not in the path."""
    work = Path("/var/lib/osm/.temp")
    assert clone_path_for(work, "PROJ-1") == work / "PROJ-1"
    assert clone_identity("PROJ-99") == "PROJ-99"


def test_clone_path_rejects_dot_slash_and_collision() -> None:
    work = Path("/tmp/osm-work-safe")
    work.mkdir(parents=True, exist_ok=True)
    from opencode_manager.git.clone import GitError
    from opencode_manager.models import validate_request_fields

    body = {
        "repo_url": "https://x/y.git",
        "source_branch": "d",
        "prompt": "p",
        "model": "a/b",
        "agent_mode": "build",
        "timeout_in_seconds": 1,
        "retry_count": 1,
        "callback_url": "http://127.0.0.1/x",
    }
    assert validate_request_fields({**body, "jira_id": "."})
    assert validate_request_fields({**body, "jira_id": "PROJ/99"})
    assert validate_request_fields({**body, "jira_id": "PROJ_99"}) is None
    try:
        clone_path_for(work, ".")
    except GitError as exc:
        assert "unsafe" in str(exc).lower() or "not under" in str(exc).lower()
    else:
        raise AssertionError("jira_id='.' must not resolve to the work root")
    dest = clone_path_for(work, "PROJ_99")
    assert dest.resolve() != work.resolve()
    assert dest.resolve().is_relative_to(work.resolve())


def test_subprocess_timeout_on_git_becomes_giterror(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("opencode_manager.git.clone.subprocess.Popen", boom)
    with pytest.raises(GitError, match="timed out"):
        _run_git(["status"], env=os.environ.copy(), timeout=1.0)


def test_subprocess_timeout_on_ls_remote_becomes_giterror(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=1)

    monkeypatch.setattr("opencode_manager.git.clone.subprocess.Popen", boom)
    with pytest.raises(GitError, match="timed out"):
        ls_remote_has_branch("https://example/r.git", "develop", pat="x", timeout=1.0)


def test_git_timeout_from_ls_remote_deletes_dest(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = _job("T-TO")
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def boom(*_a, **_k) -> bool:
        dest.mkdir(parents=True)
        (dest / "partial").write_text("x", encoding="utf-8")
        raise GitError("ls-remote timed out after 1.0s")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", boom)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert not dest.exists()


def test_serve_health_timeout_uses_all_retry_attempts(
    tmp_settings: Settings, monkeypatch
) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = _job("T-SERVE", retry_count=3)
    calls = {"n": 0}

    def boom(**_k):
        calls["n"] += 1
        raise TimeoutError("serve health not ready")

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", boom)
    with pytest.raises(JobFailed) as excinfo:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store,
            clone=clone,
            should_stop=lambda: False,
        )
    assert excinfo.value.status_code == 500
    assert calls["n"] == 3
    assert [row.kind for row in job.attempts] == ["serve-dead"] * 3


def test_serve_boot_timeout_does_not_escape_as_timeouterror(
    tmp_settings: Settings, monkeypatch
) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = _job("T-ESC", retry_count=2)

    monkeypatch.setattr(
        "opencode_manager.opencode.retry.start_serve",
        lambda **_k: (_ for _ in ()).throw(TimeoutError("health")),
    )
    with pytest.raises(JobFailed) as excinfo:
        run_opencode_job(
            job, settings=tmp_settings, store=store, clone=clone, should_stop=lambda: False
        )
    assert not isinstance(excinfo.value, TimeoutError)
    assert excinfo.value.status_code == 500


def test_serve_pid_recorded_on_spawn_before_health_fails(
    tmp_settings: Settings, monkeypatch
) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = _job("T-PID", retry_count=1)
    seen: list[int] = []

    def fake_start(*, on_spawn=None, **_k):
        handle = SimpleNamespace(pid=4242, port=59999, base_url="http://127.0.0.1:59999")
        if on_spawn is not None:
            on_spawn(handle)
            seen.append(int(job.serve_pid or 0))
        raise TimeoutError("serve health not ready")

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", fake_start)
    with pytest.raises(JobFailed):
        run_opencode_job(
            job, settings=tmp_settings, store=store, clone=clone, should_stop=lambda: False
        )
    assert seen == [4242]
    assert job.serve_pid is None


def test_start_serve_logs_command_and_timeouts(tmp_path: Path, monkeypatch, caplog) -> None:
    class FakeProc:
        pid = 88

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("opencode_manager.opencode.serve.shutil.which", lambda _n: "/opt/opencode")
    monkeypatch.setattr("opencode_manager.opencode.serve.free_port", lambda: 58888)
    monkeypatch.setattr(
        "opencode_manager.opencode.serve.subprocess.Popen", lambda *_a, **_k: FakeProc()
    )
    monkeypatch.setattr("opencode_manager.opencode.serve.wait_health", lambda *_a, **_k: {"ok": True})
    import logging

    caplog.set_level(logging.INFO)
    start_serve(
        bin_name="opencode",
        cwd=tmp_path,
        log_path=tmp_path / "s.log",
        timeout=12,
        attempt_timeout=3600,
        hang_timeout=180,
    )
    joined = " ".join(r.message for r in caplog.records)
    assert "opencode command:" in joined
    assert "/opt/opencode serve --hostname 127.0.0.1 --port 58888" in joined
    assert "attempt_timeout=3600s" in joined
    assert "health_timeout=12s" in joined
    assert "hang_timeout=180s" in joined


def test_start_serve_on_spawn_runs_before_wait_health(tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []

    class FakeProc:
        pid = 77

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("opencode_manager.opencode.serve.shutil.which", lambda _n: "/bin/true")
    monkeypatch.setattr("opencode_manager.opencode.serve.free_port", lambda: 59998)
    monkeypatch.setattr(
        "opencode_manager.opencode.serve.subprocess.Popen", lambda *_a, **_k: FakeProc()
    )

    def fake_wait(*_a, **_k):
        order.append("wait")
        raise TimeoutError("health")

    monkeypatch.setattr("opencode_manager.opencode.serve.wait_health", fake_wait)
    monkeypatch.setattr("opencode_manager.opencode.serve.stop_serve", lambda _h: order.append("kill"))

    def on_spawn(_h: ServeHandle) -> None:
        order.append("spawn")

    with pytest.raises(TimeoutError):
        start_serve(
            bin_name="opencode",
            cwd=tmp_path,
            log_path=tmp_path / "s.log",
            timeout=1,
            on_spawn=on_spawn,
        )
    assert order[:2] == ["spawn", "wait"]
    assert "kill" in order


@pytest.mark.skipif(os.name == "nt", reason="fake serve script is a POSIX shell stub")
def test_start_serve_kills_child_when_health_times_out(tmp_path: Path) -> None:
    fake = tmp_path / "fake-opencode"
    fake.write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    spawned: dict[str, int] = {}

    def on_spawn(handle) -> None:
        spawned["pid"] = handle.pid

    with pytest.raises(TimeoutError):
        start_serve(
            bin_name=str(fake),
            cwd=cwd,
            log_path=tmp_path / "serve.log",
            timeout=0.8,
            on_spawn=on_spawn,
        )
    assert "pid" in spawned
    assert not _pid_alive(spawned["pid"])


def test_wait_health_still_raises_timeouterror_on_dead_port() -> None:
    with pytest.raises(TimeoutError, match="serve health not ready"):
        wait_health("http://127.0.0.1:1", "/tmp", timeout=0.2)
