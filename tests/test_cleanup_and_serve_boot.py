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


def test_omitted_source_branch_skips_ls_remote(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_nobr",
        jira_id="T-NOBR",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="",
        status="running",
    )
    called = {"ls": 0, "clone": 0}

    def ls_must_not(*_a, **_k) -> bool:
        called["ls"] += 1
        return False

    def clone_ok(*_a, **_k) -> None:
        called["clone"] += 1

    class _Ok:
        status_code = 200
        text = "done"

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", ls_must_not)
    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr("opencode_manager.worker.run_opencode_job", lambda *_a, **_k: _Ok())
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 200
    assert called["ls"] == 0
    assert called["clone"] == 1


def test_missing_branch_404_still_deletes_leftover_dest(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_nobranch",
        jira_id="T-404",
        repo_url="https://gitlab.example/g/r.git",
        source_branch="nope",
        status="running",
    )
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


def test_instance_wait_runs_before_session_create(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = JobRecord(
        job_id="job_instwait",
        jira_id="T-INST",
        prompt="do it",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
    )
    order: list[str] = []

    class _Handle:
        pid = 4243
        port = 9
        base_url = "http://127.0.0.1:9"

    class _Client:
        def __init__(self, *_a, **_k) -> None:
            return None

        def health(self) -> bool:
            order.append("health")
            return True

        def wait_directory(self, timeout: float, should_stop=None) -> None:  # noqa: ANN001, ARG002
            order.append("wait")

        def list_known_models(self) -> list[str]:
            order.append("models")
            return ["opencode/hy3-free"]

        def resume_or_create(self, inbound, title):  # noqa: ANN001, ARG002
            order.append("create")
            raise RuntimeError("stop after create")

        def close(self) -> None:
            return None

        def abort(self, *_a, **_k) -> None:
            return None

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", lambda **_k: _Handle())
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", _Client)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)
    with pytest.raises(JobFailed):
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store,
            clone=clone,
            should_stop=lambda: False,
        )
    assert order[:4] == ["health", "wait", "models", "create"]


def test_instance_wait_timeout_is_serve_dead(tmp_settings: Settings, monkeypatch) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = JobRecord(
        job_id="job_instdead",
        jira_id="T-INSTDEAD",
        prompt="do it",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=2,
        timeout_in_seconds=30,
        status="running",
    )
    waits = {"n": 0}

    class _Handle:
        pid = 4244
        port = 9
        base_url = "http://127.0.0.1:9"

    class _Client:
        def __init__(self, *_a, **_k) -> None:
            return None

        def health(self) -> bool:
            return True

        def wait_directory(self, timeout: float, should_stop=None) -> None:  # noqa: ANN001, ARG002
            waits["n"] += 1
            raise TimeoutError("ReadTimeout")

        def list_known_models(self) -> list[str]:
            raise AssertionError("must not list models before instance is ready")

        def resume_or_create(self, *_a, **_k):
            raise AssertionError("must not POST /session before instance is ready")

        def close(self) -> None:
            return None

        def abort(self, *_a, **_k) -> None:
            return None

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", lambda **_k: _Handle())
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", _Client)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)
    with pytest.raises(JobFailed) as excinfo:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store,
            clone=clone,
            should_stop=lambda: False,
        )
    assert excinfo.value.status_code == 500
    assert waits["n"] == 2
    assert [row.kind for row in job.attempts] == ["serve-dead", "serve-dead"]
    assert "directory instance not ready" in (job.attempts[-1].error or "")


def test_empty_model_inventory_fails_job_before_prompt(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = JobRecord(
        job_id="job_emptymodel",
        jira_id="T-EMPTY",
        prompt="do it",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=3,
        timeout_in_seconds=30,
        status="running",
    )
    posted: list[str] = []

    class _Handle:
        pid = 4245
        port = 9
        base_url = "http://127.0.0.1:9"

    class _Client:
        def __init__(self, *_a, **_k) -> None:
            return None

        def health(self) -> bool:
            return True

        def list_known_models(self, *, timeout: float = 15.0) -> list[str]:  # noqa: ARG002
            return []

        def close(self) -> None:
            return None

        def abort(self, *_a, **_k) -> None:
            return None

        def resume_or_create(self, *_a, **_k):
            raise AssertionError("must not create a session when inventory is empty")

        def post_message(self, *_a, **_k) -> None:
            posted.append("nope")

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", lambda **_k: _Handle())
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", _Client)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)
    with pytest.raises(JobFailed) as excinfo:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store,
            clone=clone,
            should_stop=lambda: False,
        )
    assert excinfo.value.status_code == 500
    assert "hy3-free" in excinfo.value.message
    assert posted == []
    assert job.attempts == []


def test_unknown_model_fails_job_before_prompt(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = JobRecord(
        job_id="job_badmodel",
        jira_id="T-MODEL",
        prompt="do it",
        model="opencode/hy3-free",
        agent_mode="build",
        retry_count=3,
        timeout_in_seconds=30,
        status="running",
    )
    posted: list[str] = []

    class _Handle:
        pid = 4242
        port = 9
        base_url = "http://127.0.0.1:9"

    class _Client:
        def __init__(self, *_a, **_k) -> None:
            return None

        def health(self) -> bool:
            return True

        def list_known_models(self) -> list[str]:
            return ["opencode/ling-3.0-flash-fin-free", "opencode/mimo-v2.5-free"]

        def close(self) -> None:
            return None

        def abort(self, *_a, **_k) -> None:
            return None

        def post_message(self, *_a, **_k) -> None:
            posted.append("nope")

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", lambda **_k: _Handle())
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", _Client)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)
    with pytest.raises(JobFailed) as excinfo:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=store,
            clone=clone,
            should_stop=lambda: False,
        )
    assert excinfo.value.status_code == 500
    assert "hy3-free" in excinfo.value.message
    assert "ling-3.0-flash-fin-free" in excinfo.value.message
    assert posted == []
    assert job.attempts == []


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
