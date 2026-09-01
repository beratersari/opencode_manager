"""Remaining lock / hang / close-serve edges not covered by the first matrix."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.atomic import write_text_atomic
from opencode_manager.dashboard.store import JobStore, persist_job
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed, _inner_loop, run_opencode_job
from opencode_manager.queue import JobQueue
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


def _job(job_id: str = "job_edge") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        jira_id="EDGE-1",
        status="running",
        live=True,
        session_id="ses_edge",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        prompt="do it",
        timeout_in_seconds=30,
        retry_count=1,
    )


def _ok(**overrides):
    data = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "EDGE-OK",
        "callback_url": "",
    }
    data.update(overrides)
    return data


class FakeRunner:
    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        job.session_id = job.session_id or "ses_fake"
        job.text = "ok"
        return Terminal(200, "ok")


def test_atomic_retries_tmp_write(tmp_path: Path, monkeypatch) -> None:
    calls = {"n": 0}
    real = Path.write_text

    def flaky(self, *args, **kwargs):  # noqa: ANN001
        if self.suffix == ".tmp":
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    dest = tmp_path / "row.json"
    write_text_atomic(dest, "hello", attempts=4)
    assert dest.read_text(encoding="utf-8") == "hello"
    assert calls["n"] == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_raises_when_replace_and_inplace_fail(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "row.json"
    monkeypatch.setattr(
        "opencode_manager.atomic.os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(5, "Access is denied")),
    )
    real = Path.write_text

    def flaky(self, *args, **kwargs):  # noqa: ANN001
        if self == dest:
            raise PermissionError(5, "Access is denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    with pytest.raises(PermissionError):
        write_text_atomic(dest, "hello", attempts=2)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_try_save_false_when_disk_locked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencode_manager.atomic.os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(5, "Access is denied")),
    )
    dest_hold = {"path": None}
    real = Path.write_text

    def flaky(self, *args, **kwargs):  # noqa: ANN001
        if dest_hold["path"] is not None and self == dest_hold["path"]:
            raise PermissionError(5, "Access is denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky)
    store = JobStore(tmp_path)
    job = _job("job_try")
    dest_hold["path"] = store._path(job.job_id)
    assert store.try_save(job) is False
    assert persist_job(object(), job) is False


def test_queue_dequeue_retries_replace(tmp_path: Path, monkeypatch) -> None:
    queue = JobQueue(tmp_path / "queue.json")
    queue.enqueue({"job_id": "job_q1", "jira_id": "Q-1"})
    queue.enqueue({"job_id": "job_q2", "jira_id": "Q-2"})
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr("opencode_manager.atomic.os.replace", flaky)
    first = queue.dequeue()
    assert first["job_id"] == "job_q1"
    assert queue.peek_all()[0]["job_id"] == "job_q2"
    assert calls["n"] == 3


class _AlwaysFailList:
    def __init__(self) -> None:
        self.aborts: list[str] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
        return {"ses_edge": {"type": "busy"}}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        raise RuntimeError("list_messages down")

    def abort(self, session_id: str) -> None:
        self.aborts.append(session_id)


def test_hang_still_fires_when_never_answered_and_list_fails(tmp_settings: Settings) -> None:
    tmp_settings.hang_timeout_seconds = 0.2
    client = _AlwaysFailList()
    started = time.time()
    outcome = _inner_loop(
        job=_job(),
        client=client,
        store=JobStore(tmp_settings.job_store_dir),
        settings=tmp_settings,
        deadline=time.time() + 2.5,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "hang"
    assert time.time() - started < 1.5
    assert client.aborts == ["ses_edge"]


def test_close_serve_explosions_do_not_escape(tmp_settings: Settings, monkeypatch) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "clone"
    clone.mkdir()
    job = _job("job_close")
    job.retry_count = 1
    job.session_id = ""

    class _Handle:
        pid = 4242
        port = 9
        base_url = "http://127.0.0.1:9"

    class _Client:
        def __init__(self, *_a, **_k) -> None:
            self.health_calls = 0

        def health(self) -> bool:
            self.health_calls += 1
            return self.health_calls == 1

        def wait_directory(self, timeout: float, should_stop=None) -> None:  # noqa: ARG002
            return None

        def list_known_models(self) -> list[str]:
            return ["opencode/hy3-free"]

        def resume_or_create(self, inbound, title):  # noqa: ANN001, ARG002
            return "ses_edge", True

        def list_messages(self, session_id: str) -> list:  # noqa: ARG002
            return []

        def post_message(self, *_a, **_k) -> None:
            return None

        def status(self) -> dict:
            return {}

        def abort(self, *_a, **_k) -> None:
            raise RuntimeError("abort exploded")

        def close(self) -> None:
            raise RuntimeError("close exploded")

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", lambda **_k: _Handle())
    monkeypatch.setattr(
        "opencode_manager.opencode.retry.stop_serve",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("stop exploded")),
    )
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", _Client)
    monkeypatch.setattr(
        "opencode_manager.opencode.retry.kill_pid",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("kill exploded")),
    )

    class BoomStore(JobStore):
        def save(self, job: JobRecord) -> None:  # noqa: ARG002
            raise PermissionError(5, "Access is denied")

    with pytest.raises(JobFailed) as exc:
        run_opencode_job(
            job,
            settings=tmp_settings,
            store=BoomStore(tmp_settings.job_store_dir),
            clone=clone,
            should_stop=lambda: False,
        )
    assert exc.value.status_code == 500


def test_submit_survives_running_row_save_failure(tmp_settings: Settings, monkeypatch) -> None:
    real = JobStore.save
    calls = {"n": 0}

    def flaky(self, job: JobRecord) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(5, "Access is denied")
        return real(self, job)

    monkeypatch.setattr(JobStore, "save", flaky)
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="SAVE-RUN"))
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        deadline = time.time() + 8
        last = None
        while time.time() < deadline:
            detail = client.get(f"/api/jobs/{job_id}")
            if detail.status_code == 200:
                last = detail.json()["job"]
                if last.get("status") not in {"queued", "running"}:
                    break
            time.sleep(0.04)
        assert last is not None
        assert last["status"] == "success"
        assert client.get("/api/meta").status_code == 200
