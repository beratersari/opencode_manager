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

    monkeypatch.setattr("opencode_manager.dashboard.store.os.replace", flaky)
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
        "opencode_manager.dashboard.store.os.replace",
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
