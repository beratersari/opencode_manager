"""Job JSON save must not kill a live turn on Windows Access Denied."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import _inner_loop, _save
from opencode_manager.settings import Settings


def _job(job_id: str = "job_save1") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        jira_id="SAVE-1",
        status="running",
        live=True,
        session_id="ses_save",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        prompt="do it",
        timeout_in_seconds=30,
        retry_count=1,
    )


def test_save_retries_replace_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path)
    job = _job()
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr("opencode_manager.atomic.os.replace", flaky)
    store.save(job)
    assert calls["n"] == 3
    loaded = store.get(job.job_id)
    assert loaded is not None
    assert loaded.jira_id == "SAVE-1"
    assert not list(tmp_path.glob("*.tmp"))


def test_save_falls_back_to_inplace_write(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path)
    job = _job("job_inplace")
    monkeypatch.setattr(
        "opencode_manager.atomic.os.replace",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(5, "Access is denied")),
    )
    store.save(job)
    loaded = store.get(job.job_id)
    assert loaded is not None
    assert loaded.job_id == "job_inplace"
    assert not list(tmp_path.glob("*.tmp"))


def test_save_and_get_do_not_raise_under_readers(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = _job("job_race")
    store.save(job)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                store.get(job.job_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(20):
            job.text = f"tick {i}"
            store.save(job)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
    assert errors == []
    assert store.get(job.job_id) is not None


def test_list_all_uses_json_loads_not_model_validate_json(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path)
    store.save(_job("job_parse"))
    hits = {"json": 0, "pyd": 0}

    real_loads = __import__("json").loads

    def counted_loads(raw, *a, **k):  # noqa: ANN001
        hits["json"] += 1
        return real_loads(raw, *a, **k)

    def boom_validate_json(*_a, **_k):
        hits["pyd"] += 1
        raise AssertionError("must not use model_validate_json")

    monkeypatch.setattr("opencode_manager.dashboard.store.json.loads", counted_loads)
    monkeypatch.setattr(JobRecord, "model_validate_json", boom_validate_json)
    rows = store.list_all()
    assert [j.job_id for j in rows] == ["job_parse"]
    assert hits["json"] >= 1
    assert hits["pyd"] == 0
    assert store.get("job_parse") is not None
    assert hits["pyd"] == 0


def test_list_all_skips_oversized_job_json(tmp_path: Path, monkeypatch) -> None:
    from opencode_manager.dashboard import store as storemod

    store = JobStore(tmp_path)
    store.save(_job("job_ok"))
    ok_size = (tmp_path / "job_ok.json").stat().st_size
    huge = tmp_path / "job_huge.json"
    huge.write_text("{" + (" " * (ok_size + 32)) + "}", encoding="utf-8")
    monkeypatch.setattr(storemod, "MAX_JSON_SIZE", ok_size + 1)
    ids = {j.job_id for j in store.list_all()}
    assert "job_ok" in ids
    assert "job_huge" not in ids
    assert store.get("job_huge") is None


def test_list_all_cache_and_save_invalidates(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path)
    store.save(_job("job_cache"))
    loads = {"n": 0}
    real_loads = __import__("json").loads

    def counted_loads(raw, *a, **k):  # noqa: ANN001
        loads["n"] += 1
        return real_loads(raw, *a, **k)

    monkeypatch.setattr("opencode_manager.dashboard.store.json.loads", counted_loads)
    first = store.list_all()
    second = store.list_all()
    assert [j.job_id for j in first] == ["job_cache"]
    assert [j.job_id for j in second] == ["job_cache"]
    assert loads["n"] == 1
    store.save(_job("job_cache2"))
    third = store.list_all()
    assert {j.job_id for j in third} == {"job_cache", "job_cache2"}
    assert loads["n"] >= 3


class _BoomStore:
    def __init__(self) -> None:
        self.calls = 0

    def save(self, job: JobRecord) -> None:  # noqa: ARG002
        self.calls += 1
        raise PermissionError(5, "Access is denied")


class _BusyClient:
    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.aborts: list[str] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
        return {"ses_save": {"type": "busy"}}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        return list(self._messages)

    def abort(self, session_id: str) -> None:
        self.aborts.append(session_id)


def test_save_helper_does_not_raise() -> None:
    job = _job()
    _save(_BoomStore(), job)


def test_inner_loop_survives_store_access_denied(tmp_settings: Settings) -> None:
    tmp_settings.hang_timeout_seconds = 0.2
    messages = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do it"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1"},
            "parts": [{"type": "text", "text": "still writing"}],
        },
    ]
    client = _BusyClient(messages)
    outcome = _inner_loop(
        job=_job(),
        client=client,
        store=_BoomStore(),
        settings=tmp_settings,
        deadline=time.time() + 0.7,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "timeout"
    assert outcome != "hang"


class _FlakyListClient:
    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.calls = 0
        self.aborts: list[str] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
        return {"ses_save": {"type": "busy"}}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return list(self._messages)
        raise RuntimeError("list_messages flaked")

    def abort(self, session_id: str) -> None:
        self.aborts.append(session_id)


def test_hang_does_not_fire_when_list_fails_after_assistant(tmp_settings: Settings) -> None:
    tmp_settings.hang_timeout_seconds = 0.2
    messages = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do it"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1"},
            "parts": [{"type": "text", "text": "still writing"}],
        },
    ]
    client = _FlakyListClient(messages)
    outcome = _inner_loop(
        job=_job("job_flake"),
        client=client,
        store=_BoomStore(),
        settings=tmp_settings,
        deadline=time.time() + 0.8,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "timeout"


def test_idle_list_flake_does_not_assess_empty_or_wipe_snapshot(
    tmp_settings: Settings,
) -> None:
    from tests.job_end_helpers import assistant_msg, user_msg

    messages = [
        user_msg("u1", "do it"),
        assistant_msg("a1", "turn is done", finish="stop"),
    ]
    kept = [{"id": "keep-me", "role": "assistant", "text": "prior snapshot"}]

    class _IdleFlake:
        def __init__(self) -> None:
            self.ticks = 0
            self.list_calls = 0

        def health(self) -> bool:
            return True

        def status(self) -> dict:
            self.ticks += 1
            if self.ticks == 1:
                return {"ses_save": {"type": "busy"}}
            return {"ses_save": {"type": "idle"}}

        def list_messages(self, session_id: str) -> list:  # noqa: ARG002
            self.list_calls += 1
            if self.ticks == 2:
                raise RuntimeError("list_messages flaked")
            return list(messages)

        def abort(self, session_id: str) -> None:  # noqa: ARG002
            return None

    job = _job("job_idle_flake")
    job.chat_snapshot = list(kept)
    job.session_id = "ses_save"
    client = _IdleFlake()
    snapshots: list[list] = []

    class _RecStore(JobStore):
        def save(self, record: JobRecord) -> None:
            snapshots.append(list(record.chat_snapshot or []))
            super().save(record)

    store = _RecStore(tmp_settings.job_store_dir)
    outcome = _inner_loop(
        job=job,
        client=client,
        store=store,
        settings=tmp_settings,
        deadline=time.time() + 4.0,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=0,
        baseline_compact_n=0,
    )
    assert outcome == "success"
    assert job.text == "turn is done"
    assert client.list_calls >= 3
    assert snapshots
    assert all(row != [] for row in snapshots)


def test_queue_survives_replace_access_denied(tmp_path: Path, monkeypatch) -> None:
    from opencode_manager.queue import JobQueue

    queue = JobQueue(tmp_path / "queue.json")
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr("opencode_manager.atomic.os.replace", flaky)
    queue.enqueue({"job_id": "job_q", "jira_id": "Q-1"})
    assert queue.peek_all()[0]["job_id"] == "job_q"


def test_persist_job_and_git_track_do_not_raise() -> None:
    from opencode_manager.dashboard.store import persist_job
    from opencode_manager.git.clone import _track_pid, _untrack_pid

    job = _job("job_pid")
    store = _BoomStore()
    assert persist_job(store, job) is False
    _track_pid(job, 4242, store)
    assert 4242 in job.extra_pids
    _untrack_pid(job, 4242, store)
    assert 4242 not in job.extra_pids


def test_runner_clone_path_save_failure_still_runs(tmp_settings: Settings, monkeypatch) -> None:
    from opencode_manager.worker import OpenCodeRunner, Terminal

    monkeypatch.setattr("opencode_manager.worker.clone_repo", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: Terminal(200, "ok"),
    )
    store = _BoomStore()
    job = _job("job_run")
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)  # type: ignore[arg-type]
    assert terminal.status_code == 200
