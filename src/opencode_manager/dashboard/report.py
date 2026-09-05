"""GET /api/report-context — process extras for a client-built issue zip."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from opencode_manager import __version__
from opencode_manager.brand import APP_NAME
from opencode_manager.crash import crash_log_path
from opencode_manager.log import redact
from opencode_manager.models import utc_now
from opencode_manager.settings import Settings

_MAX_APP_LOG = 512 * 1024
_MAX_CRASH_LOG = 256 * 1024
_MAX_WRAPPER_LOG = 128 * 1024
_MAX_OPENCODE_LOG = 256 * 1024
_MAX_OPENCODE_FILES = 3
_CLI_TIMEOUT = 4.0


def build_report_context(manager: Any) -> Dict[str, Any]:
    """Safe process snapshot for the dashboard report zip. Never 500s."""
    settings: Settings = manager.settings
    running, queued = _live_counts(manager)
    return {
        "meta": {
            "app_name": APP_NAME,
            "version": __version__,
            "server_time": utc_now(),
        },
        "runtime": _runtime(settings, running=running, queued=queued),
        "settings": public_settings(settings),
        "queue": {
            "items": _queue_items(manager),
            "queued_count": queued,
        },
        "live": {"running": running, "queued": queued},
        "app_log": read_capped_text(Path(settings.app_log_path or ""), max_bytes=_MAX_APP_LOG),
        "crash_log": read_capped_text(crash_log_path(Path(settings.job_log_dir)), max_bytes=_MAX_CRASH_LOG),
        "wrapper_exit_log": read_capped_text(
            Path(settings.project_root) / "logs" / "wrapper-exit.log",
            max_bytes=_MAX_WRAPPER_LOG,
        ),
        "opencode_logs": _opencode_cli_logs(),
        "serve_logs_present": _serve_log_names(settings),
        "server_time": utc_now(),
    }


def public_settings(settings: Settings) -> Dict[str, Any]:
    """Dashboard-safe settings. OSM has no PAT field."""
    return {
        "listen_host": settings.listen_host,
        "listen_port": settings.listen_port,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "callback_timeout_seconds": settings.callback_timeout_seconds,
        "callback_retry_count": settings.callback_retry_count,
        "callback_allowed_hosts": list(settings.callback_allowed_hosts),
        "data_dir": str(settings.data_dir),
        "work_dir": str(settings.work_dir or ""),
        "job_log_dir": str(settings.job_log_dir or ""),
        "job_store_dir": str(settings.job_store_dir or ""),
        "queue_path": str(settings.queue_path or ""),
        "serve_dir": str(settings.serve_dir or ""),
        "app_log_path": str(settings.app_log_path or ""),
        "log_level": settings.log_level,
        "opencode_bin": settings.opencode_bin,
        "hang_timeout_seconds": settings.hang_timeout_seconds,
        "git_clone_timeout_seconds": settings.git_clone_timeout_seconds,
        "retry_backoff_seconds": settings.retry_backoff_seconds,
        "retry_backoff_cap_seconds": settings.retry_backoff_cap_seconds,
    }


def read_capped_text(path: Path, *, max_bytes: int) -> Dict[str, Any]:
    if not path or not Path(path).is_file():
        return {
            "text": "",
            "missing": True,
            "truncated": False,
            "path": str(path) if path else "",
        }
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return {
            "text": f"(unreadable: {exc})\n",
            "missing": False,
            "truncated": False,
            "path": str(path),
        }
    truncated = len(data) > max_bytes
    if truncated:
        data = data[-max_bytes:]
    text = redact(data.decode("utf-8", errors="replace"))
    if truncated:
        text = f"[truncated to last {max_bytes} bytes]\n{text}"
    if text and not text.endswith("\n"):
        text += "\n"
    return {
        "text": text,
        "missing": False,
        "truncated": truncated,
        "path": str(path),
    }


def _live_counts(manager: Any) -> tuple[int, int]:
    try:
        running, queued = manager.live_counts()
        return int(running), int(queued)
    except Exception:  # noqa: BLE001
        return 0, 0


def _queue_items(manager: Any) -> List[Dict[str, Any]]:
    try:
        return list(manager.queue.public_items())
    except Exception:  # noqa: BLE001
        return []


def _runtime(settings: Settings, *, running: int, queued: int) -> Dict[str, Any]:
    oc = (settings.opencode_bin or "opencode").strip() or "opencode"
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version,
        "python_executable": sys.executable,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "osm_version": __version__,
        "which": {
            "git": shutil.which("git"),
            "opencode": shutil.which(oc),
        },
        "cli_versions": {
            "git": _cli_version("git"),
            "opencode": _cli_version(oc),
        },
        "live": {"running": running, "queued": queued},
    }


def _cli_version(binary: str) -> Dict[str, Any]:
    path = shutil.which(binary) or binary
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
            env=env,
        )
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {
            "path": path,
            "exit_code": proc.returncode,
            "output": text.splitlines()[0] if text else "",
        }
    except FileNotFoundError:
        return {"path": path, "error": "not found"}
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "error": str(exc)}


def _opencode_cli_logs() -> List[Dict[str, Any]]:
    roots = [
        Path.home() / ".local" / "share" / "opencode" / "log",
        Path.home() / ".opencode" / "log",
    ]
    added: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            files = sorted(
                [p for p in root.iterdir() if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for path in files:
            if len(added) >= _MAX_OPENCODE_FILES:
                return added
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            blob = read_capped_text(path, max_bytes=_MAX_OPENCODE_LOG)
            if blob.get("missing"):
                continue
            added.append({"name": path.name, **blob})
    return added


def _serve_log_names(settings: Settings) -> List[str]:
    serve_dir = settings.serve_dir
    if not serve_dir or not Path(serve_dir).is_dir():
        return []
    try:
        names = sorted(
            p.name
            for p in Path(serve_dir).iterdir()
            if p.is_file() and p.suffix.lower() == ".log"
        )
    except OSError:
        return []
    return names[:50]



