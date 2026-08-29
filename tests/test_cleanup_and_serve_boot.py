from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import GitError, clone_path_for
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.opencode.serve import start_serve
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_giterror_after_dest_created_deletes_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_giterr",
        jira_id="T-GITERR",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        status="running",
    )
    job._pat = "secret"  # type: ignore[attr-defined]
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def ls_ok(*_a, **_k) -> bool:
        return True

    def clone_fail(*_a, **_k) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_text("half", encoding="utf-8")
        raise GitError("clone exploded after dest created")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_ok)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_fail)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "clone exploded" in terminal.text
    assert not dest.exists()


def test_git_timeout_after_dest_created_deletes_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_gittimeout",
        jira_id="T-TIMEOUT",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        status="running",
    )
    job._pat = "secret"  # type: ignore[attr-defined]
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)

    def boom(*_a, **_k) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_text("partial clone", encoding="utf-8")
        raise GitError("ls-remote timed out after 1.0s")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", boom)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "timed out" in terminal.text
    assert not dest.exists()


def test_missing_branch_404_still_deletes_leftover_dest(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_nobranch",
        jira_id="T-404",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="nope",
        status="running",
    )
    job._pat = "secret"  # type: ignore[attr-defined]
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "stale").write_text("x", encoding="utf-8")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 404
    assert not dest.exists()


def test_leftover_clone_removed_before_new_clone(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_predel",
        jira_id="T-PRE",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        status="running",
    )
    job._pat = "secret"  # type: ignore[attr-defined]
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "stale").write_text("old", encoding="utf-8")
    seen: dict[str, bool] = {}

    def clone_ok(*_a, **_k) -> None:
        seen["existed_at_clone"] = dest.exists()
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "fresh").write_text("new", encoding="utf-8")

    class _Ok:
        status_code = 200
        text = "done"

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr("opencode_manager.worker.run_opencode_job", lambda *_a, **_k: _Ok())
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert seen["existed_at_clone"] is False
    assert job.clone_path == str(dest)
    assert not dest.exists()


def test_refuse_clone_if_leftover_cannot_be_deleted(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_stuck",
        jira_id="T-STUCK",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="develop",
        status="running",
    )
    job._pat = "secret"  # type: ignore[attr-defined]
    dest = clone_path_for(tmp_settings.work_dir, job.jira_id)
    dest.mkdir(parents=True, exist_ok=True)
    cloned = {"n": 0}

    def stay(*_a, **_k) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        return False

    def clone_must_not(*_a, **_k) -> None:
        cloned["n"] += 1

    monkeypatch.setattr("opencode_manager.cleanup.end.hard_delete", stay)
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: True)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_must_not)
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "could not remove leftover clone" in terminal.text
    assert cloned["n"] == 0
    assert job.clone_path == str(dest)


def test_clone_path_is_work_dir_plus_ticket() -> None:
    work = Path("/var/lib/osm/.temp")
    dest = clone_path_for(work, "PROJ-99")
    assert dest == work / "PROJ-99"
    assert clone_path_for(work, "PROJ-99") == clone_path_for(work, "PROJ-99")
    assert clone_path_for(work, "PROJ-1") != clone_path_for(work, "PROJ-2")


def test_serve_boot_timeout_uses_retry_count(tmp_settings: Settings, monkeypatch) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = JobRecord(
        job_id="job_serveto",
        jira_id="T-SERVE",
        prompt="do it",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=3,
        timeout_in_seconds=30,
        status="running",
    )
    calls = {"n": 0}

    def boom(**_k):
        calls["n"] += 1
        raise TimeoutError("serve health not ready at http://127.0.0.1:9: boom")

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
    assert [row.kind for row in job.attempts] == ["serve-dead", "serve-dead", "serve-dead"]


@pytest.mark.skipif(os.name == "nt", reason="fake serve script is a POSIX shell stub")
def test_start_serve_kills_child_on_health_timeout(tmp_path: Path) -> None:
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
