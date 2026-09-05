"""DELETE /sessions contract: not a job, 409 while jira_id is live."""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.git.clone import clone_path_for
from opencode_manager.manager import Manager
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class FakeRunner:
    def __init__(self, result: Terminal | None = None) -> None:
        self.calls: List[str] = []
        self.result = result or Terminal(200, "assistant says hi")

    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        self.calls.append(job.job_id)
        job.session_id = job.session_id or "ses_fake"
        job.text = self.result.text
        return self.result


def _job_body(**overrides):
    data = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "PROJ-1",
        "callback_url": "http://127.0.0.1:9/wait",
    }
    data.update(overrides)
    return data


def _delete_body(**overrides):
    data = {"jira_id": "PROJ-1", "session_id": "ses_abc123"}
    data.update(overrides)
    return data


def _client(settings: Settings, runner: FakeRunner | None = None) -> TestClient:
    app = create_app(settings, runner=runner or FakeRunner())
    return TestClient(app)


class _DummyProc:
    def wait(self, timeout: Optional[float] = None) -> int:  # noqa: ARG002
        return 0


@dataclass
class _DummyHandle:
    pid: int = 0
    port: int = 1
    base_url: str = "http://127.0.0.1:1"
    proc: Any = None
    log_path: Path = Path("/dev/null")

    def __post_init__(self) -> None:
        if self.proc is None:
            self.proc = _DummyProc()


class _FakeDeleteClient:
    status = 200
    calls: List[str] = []
    wait_calls: List[float] = []
    fail_exc: Optional[BaseException] = None
    wait_exc: Optional[BaseException] = None

    def __init__(self, base_url: str, directory: str) -> None:
        self.base_url = base_url
        self.directory = directory

    def wait_directory(self, timeout: float, should_stop=None) -> None:  # noqa: ANN001, ARG002
        type(self).wait_calls.append(float(timeout))
        if self.wait_exc is not None:
            raise self.wait_exc

    def delete_session(self, session_id: str) -> Any:
        type(self).calls.append(session_id)
        if self.fail_exc is not None:
            raise self.fail_exc
        return SimpleNamespace(status_code=self.status)

    def close(self) -> None:
        return None


def _install_fake_opencode(
    manager: Manager,
    *,
    status: int = 200,
    fail_exc=None,
    wait_exc=None,
) -> _FakeDeleteClient:
    _FakeDeleteClient.status = status
    _FakeDeleteClient.calls = []
    _FakeDeleteClient.wait_calls = []
    _FakeDeleteClient.fail_exc = fail_exc
    _FakeDeleteClient.wait_exc = wait_exc
    manager._start_delete_serve = lambda **_kwargs: _DummyHandle()  # type: ignore[method-assign]
    manager._stop_delete_serve = lambda _handle: None  # type: ignore[method-assign]
    manager._open_code_client_cls = _FakeDeleteClient
    return _FakeDeleteClient


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_delete_missing_fields_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/sessions", json={})
        assert res.status_code == 400
        assert res.json()["job_id"] == ""
        assert "jira_id" in res.json()["text"]


def test_delete_missing_session_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/sessions", json={"jira_id": "PROJ-1"})
        assert res.status_code == 400
        assert "session_id" in res.json()["text"]


def test_delete_session_minus_one_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/sessions", json=_delete_body(session_id="-1"))
        assert res.status_code == 400
        assert res.json()["status_code"] == 400


def test_delete_session_not_ses_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/sessions", json=_delete_body(session_id="uuid-not-ses"))
        assert res.status_code == 400


def test_delete_unsafe_jira_400(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/sessions", json=_delete_body(jira_id="../etc"))
        assert res.status_code == 400


def test_delete_not_ready_503(tmp_settings: Settings) -> None:
    manager = Manager(tmp_settings, runner=FakeRunner())
    status, env = manager.delete_session(_delete_body())
    assert status == 503
    assert env.status_code == 503
    assert env.job_id == ""


def test_delete_409_when_job_live(tmp_settings: Settings) -> None:
    hold = threading.Event()

    class Held(FakeRunner):
        def run(self, job, *, should_stop):  # noqa: ANN001
            hold.wait(timeout=5)
            return super().run(job, should_stop=should_stop)

    with _client(tmp_settings, Held()) as client:
        first = client.post("/jobs", json=_job_body())
        assert first.status_code == 202
        job_id = first.json()["job_id"]
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 409
        assert res.json()["job_id"] == job_id
        assert res.json()["status_code"] == 409
        hold.set()


def test_delete_200_and_no_history_row(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        manager = client.app.state.manager
        _install_fake_opencode(manager, status=200)
        before = len(manager.store.list_all())
        dest = clone_path_for(tmp_settings.work_dir, "PROJ-1")
        assert not dest.exists()
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
        body = res.json()
        assert body["text"] == "session deleted"
        assert body["session_id"] == "ses_abc123"
        assert body["jira_id"] == "PROJ-1"
        assert body["job_id"] == ""
        assert body["status_code"] == 200
        assert _FakeDeleteClient.wait_calls == [manager.session_delete_health_timeout]
        assert _FakeDeleteClient.calls == ["ses_abc123"]
        assert len(manager.store.list_all()) == before
        assert not dest.exists()
        assert manager._session_deletes == set()


def test_delete_waits_directory_before_opencode_delete(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        manager = client.app.state.manager
        _install_fake_opencode(manager, status=200)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
        assert _FakeDeleteClient.wait_calls == [manager.session_delete_health_timeout]
        assert _FakeDeleteClient.calls == ["ses_abc123"]


def test_delete_wait_directory_fail_does_not_call_delete(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        _install_fake_opencode(
            client.app.state.manager,
            wait_exc=TimeoutError("opencode directory instance not ready"),
        )
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 500
        assert res.json()["status_code"] == 500
        assert "directory instance not ready" in res.json()["text"]
        assert _FakeDeleteClient.wait_calls
        assert _FakeDeleteClient.calls == []
        assert client.app.state.manager._session_deletes == set()


def test_delete_404_after_directory_ready_is_200(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        _install_fake_opencode(client.app.state.manager, status=404)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
        assert _FakeDeleteClient.wait_calls
        assert _FakeDeleteClient.calls == ["ses_abc123"]


def test_delete_404_is_idempotent_200(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        _install_fake_opencode(client.app.state.manager, status=404)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
        assert res.json()["text"] == "session deleted"


def test_delete_opencode_500_is_osm_500(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        _install_fake_opencode(client.app.state.manager, status=500)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 500
        assert res.json()["job_id"] == ""
        assert client.app.state.manager._session_deletes == set()


def test_preexisting_dest_is_not_removed(tmp_settings: Settings) -> None:
    dest = clone_path_for(tmp_settings.work_dir, "PROJ-1")
    dest.mkdir(parents=True)
    marker = dest / "keep-me.txt"
    marker.write_text("stay", encoding="utf-8")
    with _client(tmp_settings) as client:
        _install_fake_opencode(client.app.state.manager, status=200)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
        assert dest.is_dir()
        assert marker.read_text(encoding="utf-8") == "stay"


def test_overlapping_delete_and_jobs_are_409(tmp_settings: Settings) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_start(**_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return _DummyHandle()

    with _client(tmp_settings) as client:
        manager = client.app.state.manager
        manager._start_delete_serve = slow_start  # type: ignore[method-assign]
        manager._stop_delete_serve = lambda _handle: None  # type: ignore[method-assign]
        manager._open_code_client_cls = _FakeDeleteClient
        _FakeDeleteClient.status = 200
        _FakeDeleteClient.calls = []
        _FakeDeleteClient.wait_calls = []
        _FakeDeleteClient.fail_exc = None
        _FakeDeleteClient.wait_exc = None

        result: list[tuple[int, str]] = []

        def _run() -> None:
            status, env = manager.delete_session(_delete_body())
            result.append((status, env.text))

        thread = threading.Thread(target=_run)
        thread.start()
        assert entered.wait(timeout=5)
        overlap = manager.delete_session(_delete_body())
        assert overlap[0] == 409
        assert "session delete" in overlap[1].text
        job = client.post("/jobs", json=_job_body())
        assert job.status_code == 409
        assert "session delete" in job.json()["text"]
        release.set()
        thread.join(timeout=5)
        assert result == [(200, "session deleted")]
        assert manager._session_deletes == set()


def test_serve_health_timeout_500_kills_child_and_clears_guard(tmp_settings: Settings) -> None:
    fake = tmp_settings.project_root / "fake-opencode"
    fake.write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    tmp_settings.opencode_bin = str(fake)
    spawned: dict[str, int] = {}
    manager = Manager(tmp_settings, runner=FakeRunner())
    manager.ready = True
    manager.session_delete_health_timeout = 0.8
    real_start = manager._start_delete_serve

    def wrapped(**kwargs):
        def on_spawn(handle) -> None:
            spawned["pid"] = handle.pid

        kwargs["on_spawn"] = on_spawn
        return real_start(**kwargs)

    manager._start_delete_serve = wrapped  # type: ignore[method-assign]
    dest = clone_path_for(tmp_settings.work_dir, "PROJ-1")
    status, env = manager.delete_session(_delete_body())
    assert status == 500
    assert env.status_code == 500
    assert "serve boot failed" in env.text
    assert manager._session_deletes == set()
    assert not dest.exists()
    assert "pid" in spawned
    assert not _pid_alive(spawned["pid"])


def test_delete_does_not_post_callback(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[Any] = []

    def _capture(*args, **kwargs):  # noqa: ANN001
        posted.append((args, kwargs))
        return None

    monkeypatch.setattr("opencode_manager.callback.post_callback", _capture)
    monkeypatch.setattr("opencode_manager.worker.post_callback", _capture)
    with _client(tmp_settings) as client:
        _install_fake_opencode(client.app.state.manager, status=200)
        res = client.request("DELETE", "/sessions", json=_delete_body())
        assert res.status_code == 200
    assert posted == []


def test_dashboard_delete_still_405(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.request("DELETE", "/api/jobs/x")
        assert res.status_code == 405
