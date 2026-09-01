"""Regression: PLAN §8 kill/delete helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencode_manager.cleanup.end import delete_clone_path, stop_job_holders
from opencode_manager.cleanup.kill import (
    drop_git_locks,
    kill_job_tree,
    kill_pid,
    may_kill,
    parse_windows_process_json,
    process_belongs,
    ProcInfo,
    protected_pids,
    reap_path,
    reap_root_is_safe,
    text_mentions_root,
    windows_cwd_candidate,
)
from opencode_manager.cleanup.rmtree import hard_delete, win_extended_path, win_reserved_stem, windows_rd_cmd
from opencode_manager.models import JobRecord


def test_win_extended_path_and_rd_cmd() -> None:
    assert win_extended_path(r"C:\osm\.temp\TICKET") == r"\\?\C:\osm\.temp\TICKET"
    assert win_extended_path(r"\\server\share\x") == r"\\?\UNC\server\share\x"
    cmd = windows_rd_cmd(Path(r"C:\osm\.temp\TICKET"))
    assert cmd[:4] == ["cmd", "/c", "rd", "/s"]
    assert cmd[4] == "/q"
    assert cmd[5].startswith("\\\\?\\")


def test_win_reserved_names() -> None:
    assert win_reserved_stem("nul")
    assert win_reserved_stem("NUL.txt")
    assert win_reserved_stem("con")
    assert win_reserved_stem("COM1")
    assert not win_reserved_stem("null")
    assert not win_reserved_stem("readme")


def test_text_mentions_root_is_path_prefix() -> None:
    root = "/var/lib/osm/.temp"
    assert text_mentions_root("git clone https://x " + root + "/T-1", root)
    assert not text_mentions_root("git clone /var/lib/osm/.tempFOO/T-1", root)
    assert process_belongs(ProcInfo(pid=1, cwd=root + "/T-1", argv=""), root)
    assert not process_belongs(ProcInfo(pid=1, cwd="/tmp/other", argv="sleep 20"), root)


def test_parse_windows_process_json_empty_and_invalid() -> None:
    assert parse_windows_process_json("") == []
    assert parse_windows_process_json("   ") == []
    assert parse_windows_process_json("not-json") == []
    assert parse_windows_process_json("null") == []
    assert parse_windows_process_json("[]") == []
    assert parse_windows_process_json("\ufeff") == []
    assert parse_windows_process_json('{"ProcessId": 7}') == [{"pid": 7, "argv": "", "exe": ""}]
    assert parse_windows_process_json('{"ProcessId": "nope"}') == [{"pid": 0, "argv": "", "exe": ""}]
    bom = parse_windows_process_json(
        '\ufeff{"ProcessId": 8, "CommandLine": "git status", "ExecutablePath": null}'
    )
    assert bom == [{"pid": 8, "argv": "git status", "exe": ""}]
    single = parse_windows_process_json(
        '{"ProcessId": 4242, "CommandLine": "git status", "ExecutablePath": "C:\\\\git.exe"}'
    )
    assert single == [{"pid": 4242, "argv": "git status", "exe": r"C:\git.exe"}]
    rows = parse_windows_process_json(
        '[{"ProcessId": 1, "CommandLine": null, "ExecutablePath": "C:\\\\Windows\\\\System32\\\\svchost.exe"},'
        ' {"processId": "99", "commandLine": "opencode serve", "executablePath": null}]'
    )
    assert rows == [
        {"pid": 1, "argv": "", "exe": r"C:\Windows\System32\svchost.exe"},
        {"pid": 99, "argv": "opencode serve", "exe": ""},
    ]


def test_reap_root_is_safe_rejects_drive_and_shallow() -> None:
    assert not reap_root_is_safe(None)
    assert not reap_root_is_safe(Path("/"))
    assert not reap_root_is_safe(Path("C:\\"))
    assert not reap_root_is_safe(Path("C:\\osm"))
    assert reap_root_is_safe(Path(r"C:\osm\.temp"))
    assert reap_root_is_safe(Path(r"C:\osm\.temp\TEST-259"))
    assert reap_root_is_safe(Path("/var/lib/osm/.temp/T-1"))


def test_may_kill_never_allows_osm_or_system() -> None:
    assert may_kill(None) is False
    assert may_kill(0) is False
    assert may_kill(1) is False
    assert may_kill(4) is False
    assert may_kill(-1) is False
    assert may_kill("nope") is False
    assert may_kill(os.getpid()) is False
    assert may_kill(os.getppid()) is False
    assert os.getpid() in protected_pids()
    parent = os.getppid()
    if parent and parent > 4:
        assert parent in protected_pids()


def test_kill_pid_never_sends_signal_to_osm(monkeypatch) -> None:
    import opencode_manager.cleanup.kill as killmod

    spawned: list[object] = []
    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *a, **k: spawned.append(("run", a)) or SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(killmod.os, "kill", lambda *a, **k: spawned.append(("kill", a)))
    monkeypatch.setattr(killmod.os, "killpg", lambda *a, **k: spawned.append(("killpg", a)))
    kill_pid(os.getpid())
    kill_pid(os.getppid())
    kill_pid(1)
    kill_pid(4)
    kill_pid(None)
    assert spawned == []


def test_kill_job_tree_refuses_self_ppid_and_junk(monkeypatch) -> None:
    killed: list[int] = []
    import opencode_manager.cleanup.kill as killmod

    monkeypatch.setattr(killmod, "kill_pid", lambda pid: killed.append(int(pid)))
    kill_job_tree([None, 0, 1, 4, os.getpid(), os.getppid(), "nope", -3])
    assert killed == []
    assert os.getpid() in protected_pids()


def test_windows_cwd_candidate_is_clone_tools_only() -> None:
    assert windows_cwd_candidate(r"C:\Program Files\Git\cmd\git.exe", "")
    assert windows_cwd_candidate("", "opencode serve --hostname 127.0.0.1 --port 4100")
    assert not windows_cwd_candidate(
        "", r'"C:\osm\.venv\Scripts\python.exe" -m opencode_manager.app'
    )
    assert not windows_cwd_candidate("", "powershell -NoProfile -Command Get-CimInstance")
    assert not windows_cwd_candidate(r"C:\Windows\System32\cmd.exe", "")
    assert windows_cwd_candidate("", "node ./scripts/run.js")
    assert not windows_cwd_candidate(r"C:\Windows\System32\svchost.exe", "")
    assert not windows_cwd_candidate("", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    assert not windows_cwd_candidate("", r"C:\Program Files\Microsoft Office\root\Office16\OUTLOOK.EXE")
    assert not windows_cwd_candidate("", "")


def test_iter_windows_processes_does_not_snapshot(monkeypatch, tmp_path: Path) -> None:
    from opencode_manager.cleanup import kill as killmod

    spawned: list[object] = []

    def capture(*a, **k):
        spawned.append(a[0] if a else k.get("args"))
        return SimpleNamespace(returncode=0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(killmod.subprocess, "run", capture)
    monkeypatch.setattr(killmod.os, "name", "nt")
    monkeypatch.setattr(killmod, "file_holder_pids", lambda *_a, **_k: [])
    clone = tmp_path / "work" / "PROJ-12881"
    clone.mkdir(parents=True)
    job = JobRecord(job_id="job_no_snap", jira_id="PROJ-12881")

    assert killmod._windows_process_rows() == []
    assert list(killmod._iter_windows_processes()) == []
    assert killmod.reap_path(clone, protect={os.getpid()}) == 0
    assert killmod.path_has_holders(clone, protect={os.getpid()}) is False
    stop_job_holders(job, clone)

    for cmd in spawned:
        joined = " ".join(str(x) for x in (cmd or []))
        assert "Get-CimInstance" not in joined
        assert "Win32_Process" not in joined


def test_rm_session_key_buffer_is_cch_plus_one() -> None:
    from opencode_manager.cleanup import kill as killmod

    assert killmod._CCH_RM_SESSION_KEY == 32
    assert killmod._RM_SESSION_KEY_CHARS == 33
    buf = killmod._rm_session_key_buffer()
    assert len(buf) == 33


@pytest.mark.skipif(os.name != "nt", reason="Restart Manager is Windows-only")
def test_windows_restart_manager_session_key_does_not_av(tmp_path: Path) -> None:
    from opencode_manager.cleanup.kill import _rm_query_pids, _windows_restart_manager_pids

    clone = tmp_path / "PROJ-12881"
    clone.mkdir()
    assert isinstance(_rm_query_pids(clone), list)
    assert isinstance(_windows_restart_manager_pids(clone), list)


def test_restart_manager_helper_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    from opencode_manager.cleanup import kill as killmod

    monkeypatch.setattr(killmod.os, "name", "nt")
    monkeypatch.delenv("OSM_RM_INPROCESS", raising=False)
    monkeypatch.setattr(
        killmod.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=-1073741819, stdout=b"", stderr=b""),
    )
    assert killmod._windows_restart_manager_pids(tmp_path) == []


def test_stop_job_holders_windows_skips_rm_until_delete(tmp_path: Path, monkeypatch) -> None:
    from opencode_manager.cleanup import end as endmod

    called: list[str] = []
    monkeypatch.setattr(endmod.os, "name", "nt")
    monkeypatch.setattr(endmod, "kill_job_tree", lambda *_a, **_k: None)
    monkeypatch.setattr(endmod, "reap_path", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        endmod, "kill_file_holders", lambda *_a, **_k: called.append("holders") or 0
    )
    monkeypatch.setattr(
        endmod, "path_has_holders", lambda *_a, **_k: called.append("scan") or False
    )
    monkeypatch.setattr(endmod, "drop_git_locks", lambda *_a, **_k: called.append("locks"))
    clone = tmp_path / "T-1"
    clone.mkdir()
    stop_job_holders(JobRecord(job_id="job_rm1", jira_id="T-1"), clone)
    assert called == ["locks"]


@pytest.mark.skipif(os.name == "nt", reason="Windows job-end does not enumerate processes (EDR)")
def test_reap_path_kills_argv_match(tmp_path: Path) -> None:
    clone = tmp_path / "TICKET"
    clone.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time,sys; time.sleep(30)", str(clone)]
    )
    try:
        time.sleep(0.1)
        assert proc.poll() is None
        killed = reap_path(clone, protect={os.getpid()})
        assert killed >= 1
        proc.wait(timeout=2)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_drop_git_locks_only_lock_files(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    lock = git / "index.lock"
    lock.write_text("", encoding="utf-8")
    keep = git / "HEAD"
    keep.write_text("ref: refs/heads/develop", encoding="utf-8")
    drop_git_locks(tmp_path)
    assert not lock.exists()
    assert keep.exists()


def test_stop_job_holders_survives_reap_error(tmp_path: Path, monkeypatch) -> None:
    called: dict[str, bool] = {}

    def boom(*_a, **_k) -> int:
        called["reap"] = True
        raise RuntimeError("reap failed")

    def holders(*_a, **_k) -> int:
        called["holders"] = True
        return 0

    monkeypatch.setattr("opencode_manager.cleanup.end.reap_path", boom)
    monkeypatch.setattr("opencode_manager.cleanup.end.kill_file_holders", holders)
    monkeypatch.setattr("opencode_manager.cleanup.end.path_has_holders", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.drop_git_locks",
        lambda *_a, **_k: called.setdefault("locks", True),
    )
    job = JobRecord(job_id="job_x", jira_id="X-1")
    clone = tmp_path / "X-1"
    clone.mkdir()
    stop_job_holders(job, clone)
    assert called.get("reap") is True
    if os.name == "nt":
        assert called.get("holders") is None
    else:
        assert called.get("holders") is True
    assert called.get("locks") is True


def test_stop_job_holders_kills_extra_pids(tmp_path: Path) -> None:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        job = JobRecord(job_id="job_x", jira_id="X-1", extra_pids=[proc.pid])
        clone = tmp_path / "X-1"
        clone.mkdir()
        stop_job_holders(job, clone)
        proc.wait(timeout=2)
        assert proc.poll() is not None
        assert job.extra_pids == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_hard_delete_removes_tree(tmp_path: Path) -> None:
    dest = tmp_path / "gone"
    dest.mkdir()
    (dest / "f").write_text("x", encoding="utf-8")
    assert delete_clone_path(dest, reason="test")
    assert not dest.exists()
    assert hard_delete(dest)


def test_delete_skips_rm_when_folder_already_gone(tmp_path: Path, monkeypatch) -> None:
    from opencode_manager.cleanup import end as endmod
    from opencode_manager.cleanup.kill import RmHelperResult

    dest = tmp_path / "gone"
    dest.mkdir()
    monkeypatch.setattr(endmod.os, "name", "nt")
    queries: list[object] = []
    monkeypatch.setattr(
        endmod,
        "query_windows_restart_manager",
        lambda *_a, **_k: queries.append(1) or RmHelperResult(),
    )
    assert delete_clone_path(dest, reason="test")
    assert queries == []


def test_rm_retry_only_when_helper_died_and_folder_remains(
    tmp_path: Path, monkeypatch
) -> None:
    from opencode_manager.cleanup import end as endmod
    from opencode_manager.cleanup.kill import RmHelperResult

    dest = tmp_path / "stuck"
    dest.mkdir()
    monkeypatch.setattr(endmod.os, "name", "nt")
    deletes = {"n": 0}
    queries: list[RmHelperResult] = []

    def fake_delete(path):  # noqa: ANN001
        deletes["n"] += 1
        if deletes["n"] < 3:
            return False
        import shutil

        shutil.rmtree(path)
        return True

    def fake_rm(_path):
        if len(queries) == 0:
            queries.append(RmHelperResult(died=True))
        else:
            queries.append(RmHelperResult(pids=[4242], died=False))
        return queries[-1]

    killed: list[int] = []
    monkeypatch.setattr(endmod, "hard_delete", fake_delete)
    monkeypatch.setattr(endmod, "query_windows_restart_manager", fake_rm)
    monkeypatch.setattr(endmod, "may_kill", lambda pid: True)
    monkeypatch.setattr(endmod, "kill_pid", lambda pid: killed.append(int(pid)))
    assert delete_clone_path(dest, reason="retry")
    assert len(queries) == 2
    assert killed == [4242]
    assert not dest.exists()


def test_rm_no_second_child_when_helper_survives_empty(
    tmp_path: Path, monkeypatch
) -> None:
    from opencode_manager.cleanup import end as endmod
    from opencode_manager.cleanup.kill import RmHelperResult

    dest = tmp_path / "stuck2"
    dest.mkdir()
    monkeypatch.setattr(endmod.os, "name", "nt")
    monkeypatch.setattr(endmod, "hard_delete", lambda *_a, **_k: False)
    queries = {"n": 0}

    def fake_rm(_path):
        queries["n"] += 1
        return RmHelperResult(pids=[], died=False)

    monkeypatch.setattr(endmod, "query_windows_restart_manager", fake_rm)
    assert delete_clone_path(dest, reason="no-retry") is False
    assert queries["n"] == 1
    assert dest.exists()
