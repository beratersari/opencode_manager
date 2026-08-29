"""Force-kill a job process tree. Never the manager."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from opencode_manager.log import get_logger

logger = get_logger()


def kill_pid(pid: Optional[int]) -> None:
    if not pid or pid == os.getpid():
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_job_tree(pids: Iterable[Optional[int]]) -> None:
    manager = os.getpid()
    for pid in pids:
        if pid and int(pid) != manager:
            logger.info("kill process tree pid=%s", pid)
            kill_pid(int(pid))
        elif pid == manager:
            logger.warning("refusing to kill manager pid %s", manager)


def reap_work_dir(work_dir: Path) -> int:
    """Best-effort: kill leftovers whose cwd is under work_dir (Linux)."""
    if os.name == "nt":
        return 0
    root = str(work_dir.resolve())
    killed = 0
    proc = Path("/proc")
    if not proc.is_dir():
        return 0
    manager = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == manager:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + os.sep):
            kill_pid(pid)
            killed += 1
    return killed
