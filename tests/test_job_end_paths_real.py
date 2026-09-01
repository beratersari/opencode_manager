"""Real process / real git coverage of job-end kill + delete.

Fake OpenCode is used only where a live model would be required.
Git, child processes, and the clone tree are real.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opencode_manager.git.clone import GitError, clone_repo, ls_remote_has_branch, _run_git
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner

from tests.job_end_helpers import (
    dest_for,
    file_url,
    make_job,
    seed_git_repo,
    spawn_holder,
    store_for,
    wait_reaped,
)


def _attach_child(dest: Path, job) -> subprocess.Popen:
    proc = spawn_holder(dest)
    job.extra_pids.append(proc.pid)
    return proc


@pytest.mark.parametrize(
    "kind,status",
    [
        ("success", 200),
        ("giterror", 500),
        ("missing_branch", 404),
        ("git_timeout", 500),
        ("missing_branch_flag", 404),
        ("jobfailed", 500),
        ("jobfailed_504", 504),
        ("crash", 500),
        ("shutdown", 500),
    ],
)
def test_real_child_killed_and_clone_deleted_on_every_outcome(
    tmp_settings: Settings, monkeypatch, kind, status
) -> None:
    store = store_for(tmp_settings)
    job = make_job(f"R-{kind}")
    dest = dest_for(tmp_settings, job)
    child = _attach_child(dest, job)

    def ls_ok(*_a, **_k) -> bool:
        return True

    def ls_no(*_a, **_k) -> bool:
        return False

    def clone_keep(*_a, **_k) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ok").write_text("tree", encoding="utf-8")

    if kind == "success":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_keep)
        monkeypatch.setattr(
            "opencode_manager.worker.run_opencode_job",
            lambda *_a, **_k: type("R", (), {"status_code": 200, "text": "ok"})(),
        )
    elif kind == "giterror":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr(
            "opencode_manager.worker.clone_repo",
            lambda *_a, **_k: (_ for _ in ()).throw(GitError("clone exploded")),
        )
    elif kind == "missing_branch":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_no)
    elif kind == "git_timeout":
        monkeypatch.setattr(
            "opencode_manager.worker.ls_remote_has_branch",
            lambda *_a, **_k: (_ for _ in ()).throw(GitError("ls-remote timed out after 1.0s")),
        )
    elif kind == "missing_branch_flag":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr(
            "opencode_manager.worker.clone_repo",
            lambda *_a, **_k: (_ for _ in ()).throw(
                GitError("branch gone", missing_branch=True)
            ),
        )
    elif kind == "jobfailed":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_keep)
        monkeypatch.setattr(
            "opencode_manager.worker.run_opencode_job",
            lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(500, "still asking")),
        )
    elif kind == "jobfailed_504":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_keep)
        monkeypatch.setattr(
            "opencode_manager.worker.run_opencode_job",
            lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(504, "attempt clock")),
        )
    elif kind == "crash":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_keep)
        monkeypatch.setattr(
            "opencode_manager.worker.run_opencode_job",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    elif kind == "shutdown":
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_keep)
        terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: True)
        assert terminal.status_code == status
        wait_reaped(child)
        assert not dest.exists()
        return

    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == status
    wait_reaped(child)
    assert not dest.exists()


def test_real_other_jobs_child_is_not_killed(tmp_settings: Settings, monkeypatch) -> None:
    store = store_for(tmp_settings)
    job = make_job("R-SELF")
    dest = dest_for(tmp_settings, job)
    other = tmp_settings.work_dir / "OTHER-1"
    other_child = spawn_holder(other)
    self_child = spawn_holder(dest)
    job.extra_pids.append(self_child.pid)
    try:
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
        terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
        assert terminal.status_code == 404
        wait_reaped(self_child)
        assert not dest.exists()
        assert other_child.poll() is None
        assert other.exists()
    finally:
        if other_child.poll() is None:
            other_child.kill()
            other_child.wait(timeout=2)


def test_real_git_missing_branch_404_deletes_dest(tmp_settings: Settings) -> None:
    origin = seed_git_repo(tmp_settings.work_dir.parent / "origin", branch="develop")
    store = store_for(tmp_settings)
    job = make_job("R-GIT404", repo_url=file_url(origin), source_branch="does-not-exist")
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    (dest / "stale").write_text("old", encoding="utf-8")
    child = _attach_child(dest, job)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 404
    wait_reaped(child)
    assert not dest.exists()


def test_real_git_dev_does_not_match_develop(tmp_settings: Settings) -> None:
    origin = seed_git_repo(tmp_settings.work_dir.parent / "origin2", branch="develop")
    assert (
        ls_remote_has_branch(file_url(origin), "dev", timeout=15.0) is False
    )
    assert (
        ls_remote_has_branch(file_url(origin), "develop", timeout=15.0) is True
    )


def test_real_git_clone_then_opencode_error_deletes(
    tmp_settings: Settings, monkeypatch
) -> None:
    origin = seed_git_repo(tmp_settings.work_dir.parent / "origin3", branch="develop")
    store = store_for(tmp_settings)
    job = make_job("R-CLONE", repo_url=file_url(origin), source_branch="develop")
    dest = dest_for(tmp_settings, job)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: (_ for _ in ()).throw(JobFailed(500, "still asking")),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert not dest.exists()
    assert job.clone_path == str(dest)


def test_real_git_origin_scrub_keeps_port(tmp_path: Path) -> None:
    origin = seed_git_repo(tmp_path / "origin4", branch="develop")
    dest = tmp_path / "clone"
    clone_repo(file_url(origin), dest, "develop", timeout=30.0)
    _run_git(
        ["remote", "set-url", "origin", "https://oauth2:TOKEN@gitlab.example.com:8443/g/r.git"],
        env=os.environ.copy(),
        cwd=dest,
        timeout=10.0,
    )
    from opencode_manager.git.clone import _scrub_origin

    _scrub_origin(dest, os.environ.copy(), timeout=10.0)
    got = _run_git(
        ["remote", "get-url", "origin"],
        env=os.environ.copy(),
        cwd=dest,
        timeout=10.0,
    )
    url = (got.stdout or "").strip()
    assert "8443" in url
    assert "TOKEN" not in url
    assert "oauth2" not in url


def test_real_git_child_timeout_is_killed(tmp_path: Path, monkeypatch) -> None:
    sleeper = tmp_path / "git"
    sleeper.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    sleeper.chmod(sleeper.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    killed: list[int] = []
    real_kill = __import__("opencode_manager.cleanup.kill", fromlist=["kill_pid"]).kill_pid

    def spy(pid):
        killed.append(int(pid))
        real_kill(pid)

    monkeypatch.setattr("opencode_manager.git.clone.kill_pid", spy)
    with pytest.raises(GitError, match="timed out"):
        _run_git(["status"], env=env, timeout=0.4)
    assert killed
    time.sleep(0.1)


def test_real_serve_boot_fail_deletes_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = store_for(tmp_settings)
    job = make_job("R-SERVE", timeout_in_seconds=6, retry_count=1)
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    (dest / "tree.txt").write_text("x", encoding="utf-8")
    child = spawn_holder(dest)
    job.extra_pids.append(child.pid)
    script = tmp_settings.work_dir / "dead-serve"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    tmp_settings.opencode_bin = str(script)
    tmp_settings.retry_backoff_seconds = 0.0
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: dest.mkdir(parents=True, exist_ok=True),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    wait_reaped(child)
    assert not dest.exists()


def test_real_argv_holder_killed_even_if_not_in_extra_pids(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = store_for(tmp_settings)
    job = make_job("R-ARGV")
    dest = dest_for(tmp_settings, job)
    dest.mkdir()
    orphan = spawn_holder(dest)
    try:
        monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
        terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
        assert terminal.status_code == 404
        wait_reaped(orphan)
        assert not dest.exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=2)


@pytest.mark.live
def test_live_real_git_and_dead_serve_deletes(tmp_path: Path) -> None:
    """Real git clone + a non-opencode binary so serve-dead is deterministic."""
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        work_dir=tmp_path / "work",
        job_log_dir=tmp_path / "logs",
        job_store_dir=tmp_path / "jobs",
        queue_path=tmp_path / "queue.json",
        project_root=tmp_path,
        git_clone_timeout_seconds=30.0,
        retry_backoff_seconds=0.0,
        opencode_bin=str(tmp_path / "dead-serve"),
    )
    settings.ensure_dirs()
    (tmp_path / "dead-serve").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (tmp_path / "dead-serve").chmod(0o755)
    origin = seed_git_repo(tmp_path / "origin", branch="develop")
    job = make_job(
        "LIVE-DEAD",
        repo_url=file_url(origin),
        source_branch="develop",
        timeout_in_seconds=8,
        retry_count=1,
    )
    dest = dest_for(settings, job)
    terminal = OpenCodeRunner(settings, store_for(settings)).run(
        job, should_stop=lambda: False
    )
    assert terminal.status_code == 500
    assert not dest.exists()
