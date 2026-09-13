"""Queue dequeue / boot leftover must not pin a ticket 409."""

from __future__ import annotations

import threading
import time

from opencode_manager.dashboard.store import JobStore
from opencode_manager.manager import Manager
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.queue import JobQueue
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal, _finished_ids, job_already_finished


def _body(**overrides):
    data = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "Q-1",
        "callback_url": "",
    }
    data.update(overrides)
    return data


def _wait_until(pred, timeout: float = 4.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_queue_drop_removes_only_that_id(tmp_path) -> None:
    q = JobQueue(tmp_path / "queue.json")
    q.enqueue({"job_id": "job_a", "jira_id": "A-1"})
    q.enqueue({"job_id": "job_b", "jira_id": "B-1"})
    assert q.drop("job_a") is True
    left = q.peek_all()
    assert [r["job_id"] for r in left] == ["job_b"]
    assert q.drop("job_missing") is False
    assert q.drop("") is False


def test_dequeue_does_not_pop_if_save_raises(tmp_path, monkeypatch) -> None:
    q = JobQueue(tmp_path / "queue.json")
    q.enqueue({"job_id": "job_a", "jira_id": "A-1"})
    real = JobQueue._save

    def boom(self, rows):  # noqa: ANN001
        raise OSError("Access is denied")

    monkeypatch.setattr(JobQueue, "_save", boom)
    try:
        q.dequeue()
        raise AssertionError("expected OSError")
    except OSError:
        pass
    monkeypatch.setattr(JobQueue, "_save", real)
    assert [r["job_id"] for r in q.peek_all()] == ["job_a"]


def test_dequeue_persist_fail_does_not_409(tmp_settings: Settings, monkeypatch) -> None:
    tmp_settings.max_concurrent_jobs = 1
    hold = threading.Event()
    started: list[str] = []
    real_save = JobQueue._save

    def flaky(self, rows):  # noqa: ANN001
        if rows == []:
            raise OSError("Access is denied")
        return real_save(self, rows)

    class Runner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            started.append(job.jira_id)
            if job.jira_id == "A-1":
                hold.wait(timeout=5)
            return Terminal(200, "ok")

    monkeypatch.setattr(JobQueue, "_save", flaky)
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    manager = Manager(tmp_settings, runner=Runner())
    manager.ready = True
    try:
        st_a, env_a = manager.submit(_body(jira_id="A-1"))
        assert st_a == 202
        st_b, env_b = manager.submit(_body(jira_id="B-1"))
        assert st_b == 202
        assert "queued" in env_b.text.lower()
        hold.set()
        _wait_until(lambda: manager.store.live_for_jira("B-1") is None)
        assert "B-1" not in started
        row = manager.store.get(env_b.job_id)
        assert row is not None
        assert row.status == "error"
        assert row.live is False
        st_retry, env_retry = manager.submit(_body(jira_id="B-1"))
        assert st_retry == 202
        assert env_retry.job_id != env_b.job_id
    finally:
        hold.set()
        manager.stopping = True


def test_dequeue_persist_fail_starts_next_row(tmp_settings: Settings, monkeypatch) -> None:
    tmp_settings.max_concurrent_jobs = 1
    hold = threading.Event()
    started: list[str] = []
    real_save = JobQueue._save
    fail_once = {"n": True}

    def flaky(self, rows):  # noqa: ANN001
        # First write of a remainder after popping B (C still there) fails.
        if fail_once["n"] and len(rows) == 1 and rows[0].get("jira_id") == "C-1":
            fail_once["n"] = False
            raise OSError("Access is denied")
        return real_save(self, rows)

    class Runner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            started.append(job.jira_id)
            if job.jira_id == "A-1":
                hold.wait(timeout=5)
            return Terminal(200, "ok")

    monkeypatch.setattr(JobQueue, "_save", flaky)
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    manager = Manager(tmp_settings, runner=Runner())
    manager.ready = True
    try:
        assert manager.submit(_body(jira_id="A-1"))[0] == 202
        assert manager.submit(_body(jira_id="B-1"))[0] == 202
        assert manager.submit(_body(jira_id="C-1"))[0] == 202
        hold.set()
        _wait_until(lambda: "C-1" in started)
        assert "B-1" not in started
        assert manager.store.live_for_jira("B-1") is None
        retry = manager.submit(_body(jira_id="B-1"))
        assert retry[0] == 202
    finally:
        hold.set()
        manager.stopping = True


def test_boot_queue_clear_fail_does_not_resurrect_leftover(
    tmp_settings: Settings, monkeypatch
) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    leftover = JobRecord(
        job_id="job_leftover",
        jira_id="LEFT-1",
        status="queued",
        live=True,
        prompt="old",
        model="prov/id",
        agent_mode="planner",
        repo_url="https://example.com/r.git",
        timeout_in_seconds=30,
        retry_count=1,
        accepted_at=utc_now(),
    )
    store.save(leftover)
    JobQueue(tmp_settings.queue_path).enqueue(
        {
            "job_id": "job_leftover",
            "jira_id": "LEFT-1",
            "prompt": "old",
            "model": "prov/id",
            "agent_mode": "planner",
            "repo_url": "https://example.com/r.git",
            "timeout_in_seconds": 30,
            "retry_count": 1,
        }
    )
    real_clear = JobQueue.clear
    boom_once = {"n": True}

    def flaky_clear(self):  # noqa: ANN001
        if boom_once["n"]:
            boom_once["n"] = False
            raise OSError("Access is denied")
        return real_clear(self)

    started: list[str] = []

    class Runner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            started.append(job.job_id)
            return Terminal(200, "fresh")

    monkeypatch.setattr(JobQueue, "clear", flaky_clear)
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    manager = Manager(tmp_settings, runner=Runner())
    manager.boot()
    after = manager.store.get("job_leftover")
    assert after is not None
    assert after.status == "error"
    assert after.live is False
    assert manager.store.live_for_jira("LEFT-1") is None
    assert job_already_finished("job_leftover")
    st, env = manager.submit(_body(jira_id="OTHER-1"))
    assert st == 202
    _wait_until(lambda: env.job_id in started)
    time.sleep(0.3)
    assert "job_leftover" not in started
    left = manager.store.get("job_leftover")
    assert left is not None
    assert left.status == "error"
    retry = manager.submit(_body(jira_id="LEFT-1"))
    assert retry[0] == 202
    assert retry[1].job_id != "job_leftover"
    manager.stopping = True


def test_on_done_skips_finished_queue_row(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.post_callback", lambda *_a, **_k: None)
    tmp_settings.max_concurrent_jobs = 1
    started: list[str] = []
    hold = threading.Event()

    class Runner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            started.append(job.job_id)
            if job.jira_id == "FRESH-1":
                hold.wait(timeout=5)
            return Terminal(200, "ok")

    store = JobStore(tmp_settings.job_store_dir)
    dead = JobRecord(
        job_id="job_dead",
        jira_id="DEAD-1",
        status="error",
        live=False,
        prompt="old",
        model="prov/id",
        agent_mode="planner",
        repo_url="https://example.com/r.git",
        timeout_in_seconds=30,
        retry_count=1,
        accepted_at=utc_now(),
    )
    store.save(dead)
    _finished_ids.add("job_dead")
    q = JobQueue(tmp_settings.queue_path)
    q.enqueue({"job_id": "job_dead", "jira_id": "DEAD-1"})
    manager = Manager(tmp_settings, runner=Runner())
    manager.ready = True
    try:
        st, env = manager.submit(_body(jira_id="FRESH-1"))
        assert st == 202
        hold.set()
        _wait_until(lambda: env.job_id in started)
        time.sleep(0.3)
        assert "job_dead" not in started
        assert manager.store.get("job_dead").status == "error"
        assert manager.store.live_for_jira("DEAD-1") is None
    finally:
        hold.set()
        manager.stopping = True


def test_job_already_finished_helper() -> None:
    assert job_already_finished("") is False
    assert job_already_finished("job_never") is False
