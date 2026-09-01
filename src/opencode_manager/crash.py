"""Record why the manager process died. AV TerminateProcess cannot log from inside."""

from __future__ import annotations

import atexit
import faulthandler
import os
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

_clean = False
_crash_path: Optional[Path] = None
_fault_handle: Optional[TextIO] = None


def crash_log_path(job_log_dir: Path) -> Path:
    return Path(job_log_dir) / "crash.log"


def mark_clean_shutdown() -> None:
    global _clean
    _clean = True


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _append(line: str) -> None:
    if _crash_path is None:
        return
    try:
        _crash_path.parent.mkdir(parents=True, exist_ok=True)
        with _crash_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_stamp()}  {line.rstrip()}\n")
    except OSError:
        return


def install_crash_logging(job_log_dir: Path) -> Path:
    """faulthandler + uncaught hooks + atexit. Kill -9 / AV will not reach these."""
    global _crash_path, _fault_handle, _clean
    _clean = False
    path = crash_log_path(job_log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _crash_path = path
    try:
        if _fault_handle is not None:
            try:
                _fault_handle.close()
            except OSError:
                pass
        _fault_handle = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_fault_handle, all_threads=True)
    except OSError:
        _fault_handle = None

    def _excepthook(exc_type, exc, tb) -> None:  # noqa: ANN001
        _append(f"UNCAUGHT {exc_type.__name__}: {exc}")
        _append("".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        _append(f"THREAD {args.thread.name if args.thread else '?'}: {args.exc_type.__name__}: {args.exc_value}")
        if args.exc_type and args.exc_value and args.exc_traceback:
            _append("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    threading.excepthook = _thread_hook

    def _on_exit() -> None:
        if _clean:
            _append(f"clean shutdown pid={os.getpid()}")
        else:
            _append(
                f"ABRUPT EXIT pid={os.getpid()} "
                "(no Python exception recorded; AV/taskkill/native crash, or process killed)"
            )

    atexit.register(_on_exit)

    def _on_signal(signum: int, _frame: object) -> None:
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        _append(f"signal {name} pid={os.getpid()}")
        raise SystemExit(128 + int(signum))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            continue
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _on_signal)
        except (OSError, ValueError):
            pass

    _append(f"process start pid={os.getpid()} ppid={os.getppid()}")
    return path
