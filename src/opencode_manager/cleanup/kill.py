"""Force-kill a job process tree. Never the manager."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

from opencode_manager.log import get_logger, log_command, log_command_result

logger = get_logger()


@dataclass
class ProcInfo:
    pid: int
    cwd: Optional[str] = None
    argv: str = ""
    fds: List[str] = field(default_factory=list)


def kill_pid(pid: Optional[int]) -> None:
    if not pid or pid == os.getpid():
        return
    if os.name == "nt":
        cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
        log_command(logger, cmd)
        result = subprocess.run(cmd, capture_output=True, check=False, text=True)
        log_command_result(
            logger,
            cmd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            pid=pid,
        )
        return
    logger.info("command argv=kill -9 -- %s (process group SIGKILL)", pid)
    try:
        os.killpg(int(pid), signal.SIGKILL)
        logger.info("command ok killpg SIGKILL pid=%s", pid)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.info("killpg pid=%s failed (%s); trying kill", pid, type(exc).__name__)
        try:
            os.kill(int(pid), signal.SIGKILL)
            logger.info("command ok kill SIGKILL pid=%s", pid)
        except (ProcessLookupError, PermissionError, OSError) as exc2:
            logger.info("kill pid=%s already gone or denied (%s)", pid, type(exc2).__name__)


def kill_job_tree(pids: Iterable[Optional[int]]) -> None:
    manager = os.getpid()
    for pid in pids:
        if pid and int(pid) != manager:
            logger.info("kill process tree pid=%s", pid)
            kill_pid(int(pid))
        elif pid == manager:
            logger.warning("refusing to kill manager pid %s", manager)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def path_is_under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    child = _norm(path)
    base = _norm(root)
    if child == base:
        return True
    sep = os.sep
    return child.startswith(base + sep) or child.startswith(base + "/") or child.startswith(base + "\\")


def text_mentions_root(text: str, root: str) -> bool:
    """True when argv/text contains root as a path prefix (not a random substring)."""
    if not text or not root:
        return False
    hay = os.path.normcase(text)
    base = _norm(root)
    if hay == base:
        return True
    if base in hay:
        idx = 0
        while True:
            found = hay.find(base, idx)
            if found < 0:
                return False
            end = found + len(base)
            if end == len(hay) or hay[end] in "/\\ \t\"'":
                return True
            idx = found + 1
    return False


def process_belongs(proc: ProcInfo, root: str) -> bool:
    if proc.cwd and path_is_under(proc.cwd, root):
        return True
    if proc.argv and text_mentions_root(proc.argv, root):
        return True
    return False


def _is_wsl() -> bool:
    if os.name == "nt":
        return False
    try:
        rel = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        rel = ""
    return "microsoft" in rel or "wsl" in rel or Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def _iter_linux_processes(*, with_fds: bool) -> Iterator[ProcInfo]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cwd: Optional[str] = None
        argv = ""
        fds: List[str] = []
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = None
        try:
            raw = (entry / "cmdline").read_bytes()
            argv = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            argv = ""
        if with_fds:
            fd_dir = entry / "fd"
            try:
                for fd in fd_dir.iterdir():
                    try:
                        fds.append(os.readlink(fd))
                    except OSError:
                        continue
            except OSError:
                pass
        yield ProcInfo(pid=pid, cwd=cwd, argv=argv, fds=fds)


def _iter_windows_processes() -> Iterator[ProcInfo]:
    rows = _windows_process_rows()
    for row in rows:
        pid = int(row.get("pid") or 0)
        if not pid:
            continue
        argv = str(row.get("argv") or "")
        cwd = _windows_cwd(pid)
        yield ProcInfo(pid=pid, cwd=cwd, argv=argv)


def _windows_process_rows() -> List[dict]:
    """Best-effort pid + command line. Isolated so tests can stub it."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
    ]
    try:
        log_command(logger, cmd, timeout=30)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        log_command_result(logger, cmd, returncode=result.returncode, stderr=result.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("command FAIL argv=%s err=%s", " ".join(cmd[:3]), exc)
        return []
    text = (result.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "pid": item.get("ProcessId") or item.get("processId") or 0,
                "argv": item.get("CommandLine") or item.get("commandLine") or "",
            }
        )
    return out


def _windows_cwd(pid: int) -> Optional[str]:
    """Best-effort CurrentDirectory via NtQueryInformationProcess (64-bit)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid)
    )
    if not handle:
        return None

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    try:
        pbi = PROCESS_BASIC_INFORMATION()
        ret_len = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len)
        )
        if status != 0 or not pbi.PebBaseAddress:
            return None
        # PEB.ProcessParameters is at 0x20 on 64-bit.
        params_ptr = ctypes.c_void_p()
        nread = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(pbi.PebBaseAddress + 0x20),
            ctypes.byref(params_ptr),
            ctypes.sizeof(params_ptr),
            ctypes.byref(nread),
        ):
            return None
        if not params_ptr.value:
            return None
        # RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath UNICODE_STRING at 0x38.
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            ]

        us = UNICODE_STRING()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(params_ptr.value + 0x38),
            ctypes.byref(us),
            ctypes.sizeof(us),
            ctypes.byref(nread),
        ):
            return None
        if not us.Buffer or us.Length == 0:
            return None
        buf = (ctypes.c_wchar * ((us.Length // 2) + 1))()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(us.Buffer),
            buf,
            us.Length,
            ctypes.byref(nread),
        ):
            return None
        return buf.value.rstrip("\\/") or buf.value
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _iter_ps_processes() -> Iterator[ProcInfo]:
    """macOS / fallback when /proc is missing: pid + command line via `ps`."""
    cmd = ["ps", "-ax", "-o", "pid=,command="]
    try:
        log_command(logger, cmd, timeout=15)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        log_command_result(logger, cmd, returncode=result.returncode, stderr=result.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("command FAIL argv=%s err=%s", " ".join(cmd), exc)
        return
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split(None, 1)
        if not parts[0].isdigit():
            continue
        yield ProcInfo(
            pid=int(parts[0]),
            cwd=None,
            argv=parts[1] if len(parts) > 1 else "",
        )


def iter_processes(*, with_fds: bool = False) -> Iterator[ProcInfo]:
    if os.name == "nt":
        yield from _iter_windows_processes()
        return
    if sys.platform == "darwin" or not Path("/proc").is_dir():
        yield from _iter_ps_processes()
        return
    yield from _iter_linux_processes(with_fds=with_fds)


def reap_path(root: Path, *, protect: Optional[Iterable[int]] = None) -> int:
    """Kill leftovers whose cwd or argv is this path. Never the manager or protect set."""
    if not root:
        return 0
    base = str(root)
    guarded: Set[int] = {os.getpid()}
    if protect:
        guarded.update(int(p) for p in protect if p)
    killed = 0
    for proc in iter_processes(with_fds=False):
        if proc.pid in guarded:
            continue
        if process_belongs(proc, base):
            logger.info(
                "reap leftover pid=%s cwd=%s argv=%s root=%s",
                proc.pid,
                proc.cwd,
                (proc.argv or "")[:200],
                base,
            )
            kill_pid(proc.pid)
            killed += 1
    return killed


def reap_work_dir(work_dir: Path, *, protect: Optional[Iterable[int]] = None) -> int:
    """Boot hygiene: kill leftovers whose cwd/argv is work_dir (Windows and Linux)."""
    return reap_path(work_dir, protect=protect)


def _windows_restart_manager_pids(path: Path) -> List[int]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return []

    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(32)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        resources = (ctypes.c_wchar_p * 1)(str(path))
        if rstrtmgr.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
            return []

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [
                ("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME),
            ]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ("Process", RM_UNIQUE_PROCESS),
                ("strAppName", ctypes.c_wchar * 256),
                ("strServiceShortName", ctypes.c_wchar * 64),
                ("ApplicationType", wintypes.DWORD),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL),
            ]

        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reboot = wintypes.DWORD()
        rc = rstrtmgr.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reboot)
        )
        ERROR_MORE_DATA = 234
        if rc not in (0, ERROR_MORE_DATA) or needed.value == 0:
            return []
        arr = (RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        rc = rstrtmgr.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), arr, ctypes.byref(reboot)
        )
        if rc != 0:
            return []
        return [int(arr[i].Process.dwProcessId) for i in range(count.value)]
    except Exception:
        return []
    finally:
        try:
            rstrtmgr.RmEndSession(session)
        except Exception:
            pass


def file_holder_pids(root: Path) -> List[int]:
    """PIDs holding files under root. Linux /proc fd walk (not on WSL). Windows Restart Manager."""
    if os.name == "nt":
        return _windows_restart_manager_pids(root)
    if _is_wsl():
        logger.info("skip /proc fd walk on WSL for %s", root)
        return []
    pids: List[int] = []
    base = str(root)
    for proc in iter_processes(with_fds=True):
        if any(path_is_under(fd, base) for fd in proc.fds if fd):
            pids.append(proc.pid)
    return pids


def kill_file_holders(root: Path, *, protect: Optional[Iterable[int]] = None) -> int:
    guarded: Set[int] = {os.getpid()}
    if protect:
        guarded.update(int(p) for p in protect if p)
    killed = 0
    for pid in file_holder_pids(root):
        if pid in guarded:
            continue
        logger.info("kill file holder pid=%s root=%s", pid, root)
        kill_pid(pid)
        killed += 1
    return killed


def path_has_holders(root: Path, *, protect: Optional[Iterable[int]] = None) -> bool:
    guarded: Set[int] = {os.getpid()}
    if protect:
        guarded.update(int(p) for p in protect if p)
    for pid in file_holder_pids(root):
        if pid not in guarded:
            return True
    for proc in iter_processes(with_fds=False):
        if proc.pid in guarded:
            continue
        if process_belongs(proc, str(root)):
            return True
    return False


def drop_git_locks(clone: Path) -> None:
    git = clone / ".git"
    if not git.is_dir():
        return
    removed = 0
    for lock in git.rglob("*.lock"):
        try:
            lock.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("could not drop git lock %s: %s", lock, exc)
    if removed:
        logger.info("dropped %s stale git lock files under %s", removed, git)
