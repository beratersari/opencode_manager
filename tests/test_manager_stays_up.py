"""The manager process must stay up through cleanup / boot / API edge cases.

These lock the crash class from the Windows job-end process scan: a failed
git job (or a broken cleanup helper) must not take down the ASGI app.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.cleanup.kill import (
    _as_pid,
    _windows_process_rows,
    file_holder_pids,
    kill_file_holders,
    parse_windows_process_json,
    path_has_holders,
    reap_path,
    windows_cwd_candidate,
)
from opencode_manager.cleanup.end import delete_clone_path, stop_job_holders
from opencode_manager.cleanup.rmtree import hard_delete
from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import GitError
from opencode_manager.manager import Manager
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from opencode_manager.worker import OpenCodeRunner, Terminal, finish_job, run_pipeline


def _body(**overrides):
    data = {
        "repo_url": "https://hostname.company.com.tr/test/test_project.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "ollama/Restricted-Kimi-K2.6",
        "agent_mode": "planner",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "TEST-259",
        "callback_url": "",
    }
    data.update(overrides)
    return data


def _wait_job(client: TestClient, job_id: str, *, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        if res.status_code == 200:
            last = res.json()["job"]
            if last.get("status") not in {"queued", "running"}:
                return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still live: {last}")


def _assert_app_alive(client: TestClient) -> None:
    meta = client.get("/api/meta")
    assert meta.status_code == 200
    listing = client.get("/api/jobs?page=1&page_size=25")
    assert listing.status_code == 200
    assert "jobs" in listing.json()
    queue = client.get("/api/queue")
    assert queue.status_code == 200


def test_parse_windows_json_truncated_and_mixed_types() -> None:
    assert parse_windows_process_json("{") == []
    assert parse_windows_process_json("[1, 2, 3]") == []
    assert parse_windows_process_json('"just a string"') == []
    rows = parse_windows_process_json(
        json.dumps(
            [
                {"ProcessId": 10.0, "CommandLine": "git status", "ExecutablePath": r"C:\git.exe"},
                {"ProcessId": None, "CommandLine": None},
                {"not": "a process"},
                42,
            ]
        )
    )
    assert rows[0]["pid"] == 10
    assert rows[1]["pid"] == 0
    assert any(r["argv"] == "git status" for r in rows)


def test_as_pid_rejects_junk() -> None:
    assert _as_pid(None) is None
    assert _as_pid(True) is None
    assert _as_pid(0) is None
    assert _as_pid(4) is None
    assert _as_pid(-1) is None
    assert _as_pid("nope") is None
    assert _as_pid("4242") == 4242
    assert _as_pid(4242) == 4242


def test_windows_process_rows_timeout_and_oserror(monkeypatch) -> None:
    import subprocess

    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="ps", timeout=1)),
    )
    assert _windows_process_rows() == []

    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no powershell")),
    )
    assert _windows_process_rows() == []

    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"", 0, 1, "boom")),
    )
    assert _windows_process_rows() == []


def test_windows_process_rows_garbage_bytes(monkeypatch) -> None:
    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=b"\xff\xfe{", stderr=b""),
    )
    assert _windows_process_rows() == []


def test_stop_job_holders_skips_scan_when_clone_missing(tmp_path: Path, monkeypatch) -> None:
    clone = tmp_path / "work" / "PROJ-12881"
    called: list[str] = []
    monkeypatch.setattr("opencode_manager.cleanup.end.kill_job_tree", lambda *_a, **_k: called.append("tree"))
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.reap_path",
        lambda *_a, **_k: called.append("reap") or 0,
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_file_holders",
        lambda *_a, **_k: called.append("holders") or 0,
    )
    job = JobRecord(job_id="job_missing", jira_id="PROJ-12881")
    stop_job_holders(job, clone)
    assert called == ["tree"]


def test_reap_path_survives_iter_and_belongs_errors(tmp_path: Path, monkeypatch) -> None:
    clone = tmp_path / "work" / "T-1"
    clone.mkdir(parents=True)
    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(
        killmod,
        "iter_processes",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("snapshot crashed")),
    )
    assert reap_path(clone) == 0

    class Boom:
        pid = 99
        cwd = None
        argv = "x"

    def procs(**_k):
        yield Boom()
        yield Boom()

    monkeypatch.setattr(killmod, "iter_processes", procs)
    monkeypatch.setattr(
        killmod,
        "process_belongs",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("belongs")),
    )
    assert reap_path(clone) == 0


def test_reap_path_skips_unsafe_root(monkeypatch) -> None:
    import opencode_manager.cleanup.kill as killmod

    called = {"n": 0}

    def boom(**_k):
        called["n"] += 1
        return []

    monkeypatch.setattr(killmod, "iter_processes", boom)
    assert reap_path(Path("/")) == 0
    assert reap_path(Path(r"C:\osm")) == 0
    assert called["n"] == 0


def test_file_holders_and_path_has_holders_never_raise(tmp_path: Path, monkeypatch) -> None:
    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(
        killmod,
        "_windows_restart_manager_pids",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("rm dll")),
    )
    monkeypatch.setattr(
        killmod,
        "iter_processes",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("iter")),
    )
    assert file_holder_pids(tmp_path) == []
    assert kill_file_holders(tmp_path) == 0
    # Conservative: if the scan fails, assume holders remain (do not drop locks).
    assert path_has_holders(tmp_path) is True


def test_stop_job_holders_never_raises_when_every_step_fails(tmp_path: Path, monkeypatch) -> None:
    clone = tmp_path / "T-HOLD"
    clone.mkdir()
    job = JobRecord(job_id="job_hold", jira_id="T-HOLD", extra_pids=[])
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_job_tree",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("tree")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.reap_path",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reap")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_file_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("holders")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.path_has_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("scan")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.drop_git_locks",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("locks")),
    )
    stop_job_holders(job, clone)
    assert job.extra_pids == []


def test_delete_and_hard_delete_never_raise(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    assert hard_delete(missing) is True
    assert delete_clone_path(None, reason="none") is True
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / "f").write_text("x", encoding="utf-8")
    assert delete_clone_path(dest, reason="ok") is True
    assert not dest.exists()


def test_hard_delete_survives_subprocess_explosion(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "boom"
    dest.mkdir()
    (dest / "f").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "opencode_manager.cleanup.rmtree.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("rd failed")),
    )
    monkeypatch.setattr("opencode_manager.cleanup.rmtree.os.name", "nt")
    monkeypatch.setattr(
        "opencode_manager.cleanup.rmtree.shutil.rmtree",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rmtree")),
    )
    # On the patched nt path, rd raises; helper must return bool, not raise.
    assert hard_delete(dest, attempts=2) in {True, False}


def test_runner_git_fail_cleanup_explosions_still_return(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(
        job_id="job_dns",
        jira_id="TEST-259",
        repo_url="https://hostname.company.com.tr/test/test_project.git",
        source_branch="develop",
        prompt="x",
        model="ollama/x",
        agent_mode="plan",
        retry_count=1,
        timeout_in_seconds=30,
        status="running",
    )
    monkeypatch.setattr(
        "opencode_manager.worker.ls_remote_has_branch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            GitError("git failed (128): Could not resolve host: hostname.company.com.tr")
        ),
    )
    monkeypatch.setattr(
        "opencode_manager.worker.stop_job_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("holders")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.hard_delete",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("delete")),
    )
    terminal = OpenCodeRunner(tmp_settings, store).run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert "resolve host" in terminal.text or "git failed" in terminal.text.lower()


def test_pipeline_survives_finish_job_save_failure(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(job_id="job_save", jira_id="S-1", status="running", live=True)
    calls = {"n": 0}

    class BoomStore:
        def save(self, _job: JobRecord) -> None:
            calls["n"] += 1
            raise OSError("disk full")

    finish_job(job, Terminal(500, "git failed"), settings=tmp_settings, store=BoomStore())  # type: ignore[arg-type]
    assert calls["n"] == 1
    # Second finish must still be attempted by the pipeline if the first
    # marked the id; here we only assert finish_job itself does not raise.


def test_boot_never_raises_and_still_accepts(tmp_settings: Settings, monkeypatch) -> None:
    boom = {"on": True}
    orig = JobStore.list_all

    def maybe(self):  # noqa: ANN001
        if boom["on"]:
            raise RuntimeError("store down")
        return orig(self)

    monkeypatch.setattr(JobStore, "list_all", maybe)
    monkeypatch.setattr(
        "opencode_manager.manager.reap_work_dir",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reap")),
    )
    manager = Manager(tmp_settings)
    manager.boot()
    assert manager.ready is True
    boom["on"] = False
    status, env = manager.submit(_body(jira_id="BOOT-1"))
    assert status == 202
    assert env.job_id


def test_shutdown_never_raises(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_live",
            jira_id="L-1",
            status="running",
            live=True,
            extra_pids=[os.getpid()],
        )
    )
    monkeypatch.setattr(
        "opencode_manager.manager.stop_job_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    monkeypatch.setattr(
        "opencode_manager.manager.finish_job",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("finish")),
    )
    monkeypatch.setattr(
        "opencode_manager.manager.delete_clone_path",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rm")),
    )
    manager = Manager(tmp_settings)
    manager.ready = True
    manager.shutdown()
    assert manager.ready is False
    assert manager.stopping is True


def test_app_stays_up_after_git_fail_like_production(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencode_manager.worker.ls_remote_has_branch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            GitError("git failed (128): Could not resolve host: hostname.company.com.tr")
        ),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        first = client.post("/jobs", json=_body(jira_id="TEST-259"))
        assert first.status_code == 202
        job = _wait_job(client, first.json()["job_id"])
        assert job["status"] == "error"
        _assert_app_alive(client)
        listing = client.get("/api/jobs?page=1&page_size=25")
        assert listing.json()["total"] >= 1
        second = client.post("/jobs", json=_body(jira_id="TEST-260"))
        assert second.status_code == 202
        _wait_job(client, second.json()["job_id"])
        _assert_app_alive(client)


def test_app_stays_up_when_cleanup_helpers_raise(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.reap_path",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reap")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_file_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rm")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.path_has_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("holders")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.kill._windows_cwd",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("peb")),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_body(jira_id="TEST-261"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "not_found"
        _assert_app_alive(client)
        poll = client.get(f"/jobs/{res.json()['job_id']}")
        assert poll.status_code == 200


def test_app_survives_corrupt_store_and_queue(tmp_settings: Settings) -> None:
    tmp_settings.job_store_dir.mkdir(parents=True, exist_ok=True)
    (tmp_settings.job_store_dir / "trash.json").write_text("{not json", encoding="utf-8")
    (tmp_settings.job_store_dir / "ok.json").write_text(
        JobRecord(job_id="job_ok", jira_id="OK-1", status="success", live=False).model_dump_json(),
        encoding="utf-8",
    )
    tmp_settings.queue_path.write_text("not-a-list", encoding="utf-8")
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        _assert_app_alive(client)
        listing = client.get("/api/jobs")
        ids = [j["job_id"] for j in listing.json()["jobs"]]
        assert "job_ok" in ids
        assert client.get("/api/queue").json()["queued_count"] == 0


def test_app_survives_malformed_inbound(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        assert client.post("/jobs", content=b"not-json", headers={"content-type": "application/json"}).status_code in {
            400,
            202,
        }
        assert client.post("/jobs", json=[]).status_code == 400
        assert client.post("/jobs", json={"jira_id": "X"}).status_code == 400
        assert client.request("DELETE", "/sessions", json={}).status_code == 400
        _assert_app_alive(client)


def test_dashboard_reads_during_cleanup(tmp_settings: Settings, monkeypatch) -> None:
    hold = threading.Event()
    released = threading.Event()

    def slow_ls(*_a, **_k) -> bool:
        hold.set()
        released.wait(timeout=2)
        raise GitError("git failed (128): Could not resolve host")

    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", slow_ls)
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_body(jira_id="TEST-262"))
        assert res.status_code == 202
        assert hold.wait(timeout=2)
        for _ in range(8):
            _assert_app_alive(client)
            time.sleep(0.02)
        released.set()
        _wait_job(client, res.json()["job_id"])
        _assert_app_alive(client)


def test_windows_cwd_not_queried_for_system_while_app_runs(tmp_settings: Settings, monkeypatch) -> None:
    cwd_hits: list[int] = []
    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(
        killmod,
        "_windows_process_rows",
        lambda: [
            {"pid": 4, "argv": "", "exe": r"C:\Windows\System32\smss.exe"},
            {"pid": 88, "argv": "", "exe": r"C:\Windows\System32\svchost.exe"},
            {"pid": 99, "argv": "opencode serve --port 1", "exe": r"C:\Users\x\.opencode\bin\opencode.exe"},
            {"pid": 100, "argv": r"C:\Program Files\Google\Chrome\Application\chrome.exe", "exe": ""},
        ],
    )
    monkeypatch.setattr(killmod, "_windows_cwd", lambda pid: cwd_hits.append(pid) or r"C:\osm\.temp\TEST-259")
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)

    # Force the Windows iterator during job-end reap.
    monkeypatch.setattr(killmod.os, "name", "nt")

    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_body(jira_id="TEST-263"))
        assert res.status_code == 202
        _wait_job(client, res.json()["job_id"])
        _assert_app_alive(client)
    assert set(cwd_hits) == {99}
    assert windows_cwd_candidate(r"C:\Windows\System32\svchost.exe", "") is False


def test_run_pipeline_survives_runner_and_cleanup_raise(tmp_settings: Settings, monkeypatch) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    job = JobRecord(job_id="job_pipe", jira_id="P-1", status="queued", live=True)

    class Boom:
        def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
            raise RuntimeError("runner exploded")

    monkeypatch.setattr(
        "opencode_manager.worker.stop_job_holders",
        lambda *_a, **_k: None,
    )
    run_pipeline(
        job,
        settings=tmp_settings,
        store=store,
        runner=Boom(),
        should_stop=lambda: False,
        send_callback=False,
    )
    saved = store.get(job.job_id)
    assert saved is not None
    assert saved.status == "error"
    assert saved.live is False
