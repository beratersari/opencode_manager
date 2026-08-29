"""Regression: PLAN §8 kill/delete helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from opencode_manager.cleanup.end import delete_clone_path, stop_job_holders
from opencode_manager.cleanup.kill import (
    drop_git_locks,
    process_belongs,
    ProcInfo,
    reap_path,
    text_mentions_root,
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
