"""Force-kill a job process tree. Never the manager."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

from opencode_manager.log import get_logger, log_command, log_command_result

logger = get_logger()

# PEB / RTL_USER_PROCESS_PARAMETERS offsets (natural alignment).
_PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
_PEB_PROCESS_PARAMETERS_OFFSET = 0x20 if _PTR_SIZE == 8 else 0x10
_RTL_CURDIR_OFFSET = 0x38 if _PTR_SIZE == 8 else 0x24
_CWD_READ_CAP = 32768

# Only these images get a Windows cwd (PEB) read. Opening every PID
# with PROCESS_VM_READ looks like malware; corporate EDR kills us.
# Never PEB-read the manager tree: python / cmd / powershell host
# start-backend.bat and this process. EDR kills OSM on those opens.
_CLONE_TOOL_STEMS = frozenset(
    {
        "git",
        "opencode",
        "node",
        "bun",
        "deno",
        "rg",
        "fd",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "cargo",
        "go",
        "java",
        "gradle",
        "bash",
        "sh",
        "busybox",
    }
)
_HOST_SHELL_STEMS = frozenset(
    {"python", "python3", "py", "cmd", "powershell", "pwsh"}
)


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


_kernel32 = None
_ntdll = None
_rstrtmgr = None


_IMAGE_RE = re.compile(r"([^\\/\"'\s]+)\.(?:exe|cmd|bat|com)\b", re.IGNORECASE)


def _stem_from_filename(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _image_stem(path_or_argv: str) -> str:
    """Image basename without .exe/.cmd/.bat/.com (quoted, unquoted, or argv)."""
    if not path_or_argv:
        return ""
    text = path_or_argv.strip()
    if text[:1] in {'"', "'"}:
        quote = text[0]
        end = text.find(quote, 1)
        token = text[1:end] if end > 1 else text[1:]
        return _stem_from_filename(token)
    match = _IMAGE_RE.search(text.replace("/", "\\"))
    if match:
        return match.group(1).lower()
    token = text.split(None, 1)[0] if text else ""
    return _stem_from_filename(token)


def windows_cwd_candidate(exe: str, argv: str) -> bool:
    """True when this image might be a leftover in a clone (git/serve/tools)."""
    stems = {_image_stem(exe), _image_stem(argv)}
    if stems & _HOST_SHELL_STEMS:
        return False
    return bool(stems & _CLONE_TOOL_STEMS)


def parse_windows_process_json(text: str) -> List[dict]:
    """Parse Get-CimInstance ConvertTo-Json into {pid, argv, exe} rows."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pid_raw = item.get("ProcessId")
        if pid_raw is None:
            pid_raw = item.get("processId")
        try:
            pid = int(pid_raw or 0)
        except (TypeError, ValueError):
            pid = 0
        argv = item.get("CommandLine")
        if argv is None:
            argv = item.get("commandLine")
        exe = item.get("ExecutablePath")
        if exe is None:
            exe = item.get("executablePath")
        out.append({"pid": pid, "argv": argv or "", "exe": exe or ""})
    return out


@dataclass
class ProcInfo:
    pid: int
    cwd: Optional[str] = None
    argv: str = ""
    fds: List[str] = field(default_factory=list)


def _as_pid(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        pid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pid <= 4:
        return None
    return pid


def _parent_pid(pid: int) -> Optional[int]:
    """Immediate parent of pid. Never raises. Own pid uses getppid()."""
    try:
        if int(pid) == os.getpid():
            ppid = os.getppid()
            return int(ppid) if ppid and int(ppid) > 4 else None
    except Exception:  # noqa: BLE001
        pass
    if os.name != "nt":
        try:
            stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
            # pid (comm) state ppid ...
            close = stat.rfind(")")
            parts = stat[close + 1 :].split()
            ppid = int(parts[1])
            return ppid if ppid > 4 else None
        except Exception:  # noqa: BLE001
            return None
    try:
        from ctypes import wintypes

        kernel32, ntdll = _win_k32_ntdll()
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return None
        try:
            pbi = _PROCESS_BASIC_INFORMATION()
            ret_len = wintypes.ULONG()
            status = ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len)
            )
            if status != 0:
                return None
            parent = int(pbi.Reserved3 or 0)
            return parent if parent > 4 else None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def protected_pids() -> Set[int]:
    """This process and every ancestor. Never taskkill / SIGKILL these."""
    out: Set[int] = set()
    try:
        current = os.getpid()
    except Exception:  # noqa: BLE001
        return out
    for _ in range(16):
        try:
            pid = int(current)
        except (TypeError, ValueError):
            break
        if pid <= 4 or pid in out:
            break
        out.add(pid)
        nxt = _parent_pid(pid)
        if not nxt:
            break
        current = nxt
    return out


def may_kill(pid: Optional[int]) -> bool:
    """False for junk, system PIDs, this process, or any ancestor. All kill paths use this."""
    resolved = _as_pid(pid)
    if not resolved:
        return False
    try:
        if resolved == os.getpid():
            return False
        if resolved == os.getppid():
            return False
    except Exception:  # noqa: BLE001
        return False
    if resolved in protected_pids():
        return False
    return True


def reap_root_is_safe(root: Optional[Path]) -> bool:
    """False for missing, drive root, or a single-component path (C:\\osm)."""
    if root is None:
        return False
    try:
        text = os.path.normpath(str(root)).strip()
    except Exception:  # noqa: BLE001
        return False
    if not text or text in {os.sep, "/", "\\"}:
        return False
    # Windows drive paths must be recognized even when unit tests run on POSIX.
    if len(text) >= 2 and text[1] == ":":
        rest = text[2:].replace("\\", "/").strip("/")
        parts = [p for p in rest.split("/") if p and p not in {".", ".."}]
        return len(parts) >= 2
    _drive, tail = os.path.splitdrive(text)
    parts = [p for p in tail.replace("\\", "/").split("/") if p and p not in {".", ".."}]
    return len(parts) >= 2


def kill_pid(pid: Optional[int]) -> None:
    """Kill one job child. Never this manager or its parents. Only kill entry point."""
    resolved = _as_pid(pid)
    if not resolved:
        return
    if not may_kill(resolved):
        logger.warning("refusing to kill protected pid %s (never kill OSM)", resolved)
        return
    try:
        if os.name == "nt":
            cmd = ["taskkill", "/F", "/T", "/PID", str(resolved)]
            log_command(logger, cmd)
            result = subprocess.run(cmd, capture_output=True, check=False)
            log_command_result(
                logger,
                cmd,
                returncode=result.returncode,
                stdout=_decode_windows_stdout(result.stdout or b""),
                stderr=_decode_windows_stdout(result.stderr or b""),
                pid=resolved,
            )
            return
        logger.info("command argv=kill -9 -- %s (process group SIGKILL)", resolved)
        try:
            os.killpg(resolved, signal.SIGKILL)
            logger.info("command ok killpg SIGKILL pid=%s", resolved)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.info("killpg pid=%s failed (%s); trying kill", resolved, type(exc).__name__)
            try:
                os.kill(resolved, signal.SIGKILL)
                logger.info("command ok kill SIGKILL pid=%s", resolved)
            except (ProcessLookupError, PermissionError, OSError) as exc2:
                logger.info("kill pid=%s already gone or denied (%s)", resolved, type(exc2).__name__)
    except Exception:  # noqa: BLE001
        logger.exception("kill_pid failed pid=%s", resolved)


def kill_job_tree(pids: Iterable[Optional[int]]) -> None:
    guarded = protected_pids()
    try:
        seq = list(pids or [])
    except Exception:  # noqa: BLE001
        return
    for raw in seq:
        pid = _as_pid(raw)
        if not pid:
            continue
        if not may_kill(pid) or pid in guarded:
            logger.warning("refusing to kill protected pid %s (never kill OSM)", pid)
            continue
        try:
            logger.info("kill process tree pid=%s", pid)
            kill_pid(pid)
        except Exception:  # noqa: BLE001
            logger.exception("kill_job_tree skip pid=%s", pid)


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
    """Do not enumerate every Win32_Process.

    `Get-CimInstance Win32_Process` via PowerShell (and PEB-walking the
    result) is what EDR treats as malware. After a *successful* job the
    clone exists, job-end used to scan twice, then the manager process
    vanished (`Backend exited`) even though the job was 200.
    Windows leftovers are Restart Manager file holders only.
    """
    logger.info("windows leftover process snapshot skipped (EDR-safe)")
    return
    yield  # pragma: no cover — keep this a generator


def _decode_windows_stdout(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "utf-16-le"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _windows_process_rows() -> List[dict]:
    """Best-effort pid + command line + exe. Isolated so tests can stub it."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine,ExecutablePath | ConvertTo-Json -Compress",
    ]
    try:
        log_command(logger, cmd, timeout=30)
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
            check=False,
        )
        stderr = _decode_windows_stdout(result.stderr or b"")
        log_command_result(
            logger,
            cmd,
            returncode=result.returncode,
            stderr=stderr,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("command FAIL argv=%s err=%s", " ".join(cmd[:3]), exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.error("windows process snapshot failed err=%s", exc)
        return []
    text = _decode_windows_stdout(result.stdout or b"")
    rows = parse_windows_process_json(text)
    logger.info(
        "windows process snapshot bytes=%s rows=%s",
        len(result.stdout or b""),
        len(rows),
    )
    return rows


def _win_k32_ntdll():
    """kernel32/ntdll with 64-bit HANDLE/SIZE_T prototypes (not default c_int)."""
    global _kernel32, _ntdll
    if _kernel32 is not None and _ntdll is not None:
        return _kernel32, _ntdll
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _kernel32, _ntdll = kernel32, ntdll
    return kernel32, ntdll


def _windows_cwd(pid: int) -> Optional[str]:
    """Best-effort CurrentDirectory via prototyped NtQuery/ReadProcessMemory."""
    if os.name != "nt":
        return None
    if not pid or int(pid) <= 4 or int(pid) in protected_pids():
        return None
    try:
        from ctypes import wintypes
    except Exception:
        return None

    try:
        kernel32, ntdll = _win_k32_ntdll()
    except Exception:
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid)
    )
    if not handle:
        return None

    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        ret_len = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len)
        )
        if status != 0 or not pbi.PebBaseAddress:
            return None
        peb = int(pbi.PebBaseAddress)
        params_ptr = ctypes.c_void_p()
        nread = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(peb + _PEB_PROCESS_PARAMETERS_OFFSET),
            ctypes.byref(params_ptr),
            ctypes.sizeof(params_ptr),
            ctypes.byref(nread),
        ):
            return None
        if not params_ptr.value:
            return None
        us = _UNICODE_STRING()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(params_ptr.value) + _RTL_CURDIR_OFFSET),
            ctypes.byref(us),
            ctypes.sizeof(us),
            ctypes.byref(nread),
        ):
            return None
        if not us.Buffer or us.Length == 0:
            return None
        n_bytes = int(us.Length)
        if n_bytes <= 0 or n_bytes > _CWD_READ_CAP:
            return None
        buf = (ctypes.c_wchar * ((n_bytes // 2) + 1))()
        if not kernel32.ReadProcessMemory(
            handle,
            ctypes.c_void_p(int(us.Buffer)),
            buf,
            n_bytes,
            ctypes.byref(nread),
        ):
            return None
        return buf.value.rstrip("\\/") or buf.value
    except Exception:
        return None
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


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
    if not reap_root_is_safe(root):
        logger.warning("reap_path skip unsafe root=%s", root)
        return 0
    base = str(root)
    guarded: Set[int] = set(protected_pids())
    if protect:
        for p in protect:
            pid = _as_pid(p)
            if pid:
                guarded.add(pid)
    killed = 0
    try:
        procs = list(iter_processes(with_fds=False))
    except Exception:  # noqa: BLE001
        logger.exception("iter_processes failed root=%s", base)
        return 0
    for proc in procs:
        try:
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
        except Exception:  # noqa: BLE001
            logger.exception("reap_path skip pid=%s", getattr(proc, "pid", "?"))
    return killed


def reap_work_dir(work_dir: Path, *, protect: Optional[Iterable[int]] = None) -> int:
    """Boot hygiene: kill leftovers whose cwd/argv is work_dir (Windows and Linux)."""
    return reap_path(work_dir, protect=protect)


def _win_rstrtmgr():
    """Restart Manager with prototyped DWORD/pointer args (not default c_int)."""
    global _rstrtmgr
    if _rstrtmgr is not None:
        return _rstrtmgr
    from ctypes import wintypes

    rstrtmgr = ctypes.WinDLL("rstrtmgr")
    rstrtmgr.RmStartSession.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    rstrtmgr.RmStartSession.restype = wintypes.DWORD
    rstrtmgr.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_wchar_p),
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
    ]
    rstrtmgr.RmRegisterResources.restype = wintypes.DWORD
    rstrtmgr.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    rstrtmgr.RmGetList.restype = wintypes.DWORD
    rstrtmgr.RmEndSession.argtypes = [wintypes.DWORD]
    rstrtmgr.RmEndSession.restype = wintypes.DWORD
    _rstrtmgr = rstrtmgr
    return rstrtmgr


def _windows_restart_manager_pids(path: Path) -> List[int]:
    if os.name != "nt":
        return []
    try:
        from ctypes import wintypes
    except Exception:
        return []
    try:
        rstrtmgr = _win_rstrtmgr()
    except OSError:
        return []

    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(32)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    handle = int(session.value)
    try:
        resources = (ctypes.c_wchar_p * 1)(str(path))
        if rstrtmgr.RmRegisterResources(handle, 1, resources, 0, None, 0, None) != 0:
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
            handle, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reboot)
        )
        ERROR_MORE_DATA = 234
        if rc not in (0, ERROR_MORE_DATA) or needed.value == 0:
            return []
        arr = (RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        rc = rstrtmgr.RmGetList(
            handle, ctypes.byref(needed), ctypes.byref(count), arr, ctypes.byref(reboot)
        )
        if rc != 0:
            return []
        return [int(arr[i].Process.dwProcessId) for i in range(count.value)]
    except Exception:
        return []
    finally:
        try:
            rstrtmgr.RmEndSession(handle)
        except Exception:
            pass


def _guard_set(protect: Optional[Iterable[int]] = None) -> Set[int]:
    guarded = set(protected_pids())
    if protect:
        for p in protect:
            pid = _as_pid(p)
            if pid:
                guarded.add(pid)
    return guarded


def file_holder_pids(root: Path) -> List[int]:
    """PIDs holding files under root. Linux /proc fd walk (not on WSL). Windows Restart Manager."""
    try:
        if os.name == "nt":
            return _windows_restart_manager_pids(root)
        if _is_wsl():
            logger.info("skip /proc fd walk on WSL for %s", root)
            return []
        pids: List[int] = []
        base = str(root)
        for proc in iter_processes(with_fds=True):
            try:
                if any(path_is_under(fd, base) for fd in proc.fds if fd):
                    pids.append(proc.pid)
            except Exception:  # noqa: BLE001
                continue
        return pids
    except Exception:  # noqa: BLE001
        logger.exception("file_holder_pids failed root=%s", root)
        return []


def kill_file_holders(root: Path, *, protect: Optional[Iterable[int]] = None) -> int:
    guarded = _guard_set(protect)
    killed = 0
    for raw in file_holder_pids(root):
        try:
            pid = _as_pid(raw)
            if not pid or pid in guarded:
                continue
            logger.info("kill file holder pid=%s root=%s", pid, root)
            kill_pid(pid)
            killed += 1
        except Exception:  # noqa: BLE001
            logger.exception("kill_file_holders skip pid=%s", raw)
    return killed


def path_has_holders(root: Path, *, protect: Optional[Iterable[int]] = None) -> bool:
    guarded = _guard_set(protect)
    try:
        for raw in file_holder_pids(root):
            pid = _as_pid(raw)
            if pid and pid not in guarded:
                return True
        for proc in iter_processes(with_fds=False):
            if proc.pid in guarded:
                continue
            if process_belongs(proc, str(root)):
                return True
    except Exception:  # noqa: BLE001
        logger.exception("path_has_holders failed root=%s", root)
        return True
    return False


def drop_git_locks(clone: Path) -> None:
    try:
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
    except Exception:  # noqa: BLE001
        logger.exception("drop_git_locks failed clone=%s", clone)
