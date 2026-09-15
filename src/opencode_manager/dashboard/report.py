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
from opencode_manager.crash import crash_log_path, wrapper_exit_log_path
from opencode_manager.log import redact
from opencode_manager.models import utc_now
from opencode_manager.settings import Settings

_MAX_APP_LOG = 2 * 1024 * 1024
_MAX_CRASH_LOG = 512 * 1024
_MAX_WRAPPER_LOG = 256 * 1024
_MAX_OPENCODE_LOG = 512 * 1024
_MAX_OPENCODE_FILES = 8
_MAX_SERVICE_LOG = 128 * 1024
_MAX_SERVICE_FILES = 6
_MAX_RECENT_JOBS = 40
_MAX_LOG_FILE_NAMES = 80
_MAX_SERVE_LOG_NAMES = 200
_CLI_TIMEOUT = 4.0


def build_report_context(manager: Any) -> Dict[str, Any]:
    """Safe process snapshot for the dashboard report zip. Never 500s."""
    settings: Settings = manager.settings
    running, queued = _live_counts(manager)
    n8n_running, n8n_queued, review_running, review_queued = _split_live(manager)
    jobs_summary = _jobs_summary(manager)
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
            "queued_count": n8n_queued,
        },
        "review_queue": {
            "items": _review_queue_items(manager),
            "queued_count": review_queued,
        },
        "live": {
            "running": running,
            "queued": queued,
            "n8n_running": n8n_running,
            "n8n_queued": n8n_queued,
            "review_running": review_running,
            "review_queued": review_queued,
        },
        "manager": _manager_state(
            manager,
            n8n_running=n8n_running,
            n8n_queued=n8n_queued,
            review_running=review_running,
            review_queued=review_queued,
        ),
        "layout": _layout(settings),
        "jobs_summary": jobs_summary,
        "app_log": read_capped_text(Path(settings.app_log_path or ""), max_bytes=_MAX_APP_LOG),
        "crash_log": read_capped_text(crash_log_path(Path(settings.job_log_dir)), max_bytes=_MAX_CRASH_LOG),
        "wrapper_exit_log": read_capped_text(
            _wrapper_exit_path(settings),
            max_bytes=_MAX_WRAPPER_LOG,
        ),
        "opencode_logs": _opencode_cli_logs(),
        "service_logs": _service_logs(settings),
        "log_files_present": _log_file_names(settings),
        "serve_logs_present": _serve_log_names(settings),
        "serve_logs": _serve_log_rows(settings),
        "server_time": utc_now(),
    }


def _wrapper_exit_path(settings: Settings) -> Path:
    """Prefer {data_dir}/logs/wrapper-exit.log; keep project/logs as a leftover."""
    primary = wrapper_exit_log_path(Path(settings.job_log_dir or settings.data_dir / "logs"))
    if primary.is_file():
        return primary
    legacy = Path(settings.project_root) / "logs" / "wrapper-exit.log"
    if legacy.is_file():
        return legacy
    return primary


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
        "azure_api_version": settings.azure_api_version,
        "review_serve_health_timeout": settings.review_serve_health_timeout,
        "gitlab_url": settings.gitlab_url,
        "gitlab_token_set": bool(settings.gitlab_token),
        "gitlab_webhook_secret_set": bool(settings.gitlab_webhook_secret),
        "azure_url": settings.azure_url,
        "azure_token_set": bool(settings.azure_token),
        "azure_webhook_user": settings.azure_webhook_user,
        "azure_webhook_password_set": bool(settings.azure_webhook_password),
        "skip_draft_mrs": settings.skip_draft_mrs,
        "review_mention": settings.review_mention,
        "review_model": settings.review_model,
        "review_timeout_seconds": settings.review_timeout_seconds,
        "review_retry_count": settings.review_retry_count,
        "review_agent": settings.review_agent,
        "max_concurrent_reviews": settings.max_concurrent_reviews,
        "dashboard_user": settings.dashboard_user,
        "dashboard_password_set": bool(settings.dashboard_password),
        "dashboard_token_set": bool(settings.dashboard_token),
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
        "frozen": bool(getattr(sys, "frozen", False)),
        "osm_data_dir_env_set": bool((os.environ.get("OSM_DATA_DIR") or "").strip()),
        "data_dir_writable": _writable(settings.data_dir),
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
    return names[:_MAX_SERVE_LOG_NAMES]


def _writable(path: Path) -> bool:
    try:
        return bool(path) and Path(path).exists() and os.access(path, os.W_OK)
    except OSError:
        return False


def _split_live(manager: Any) -> tuple[int, int, int, int]:
    n8n_run = 0
    n8n_q = 0
    rev_run = 0
    rev_q = 0
    try:
        with manager._lock:
            n8n_run = int(getattr(manager, "_running", 0) or 0)
    except Exception:  # noqa: BLE001
        n8n_run = 0
    try:
        n8n_q = len(manager.queue.peek_all())
    except Exception:  # noqa: BLE001
        n8n_q = 0
    reviews = getattr(manager, "reviews", None)
    if reviews is not None:
        try:
            rev_run, rev_q = reviews.live_counts()
        except Exception:  # noqa: BLE001
            rev_run, rev_q = 0, 0
    return n8n_run, n8n_q, int(rev_run), int(rev_q)


def _manager_state(
    manager: Any,
    *,
    n8n_running: int,
    n8n_queued: int,
    review_running: int,
    review_queued: int,
) -> Dict[str, Any]:
    return {
        "ready": bool(getattr(manager, "ready", False)),
        "stopping": bool(getattr(manager, "stopping", False)),
        "n8n_running": n8n_running,
        "n8n_queued": n8n_queued,
        "review_running": review_running,
        "review_queued": review_queued,
    }


def _review_queue_items(manager: Any) -> List[Dict[str, Any]]:
    reviews = getattr(manager, "reviews", None)
    if reviews is None:
        return []
    try:
        return list(reviews.queue.public_items())
    except Exception:  # noqa: BLE001
        return []


def _job_summary_row(job: Any) -> Dict[str, Any]:
    try:
        data = job.public_dict()
    except Exception:  # noqa: BLE001
        return {
            "job_id": getattr(job, "job_id", ""),
            "jira_id": getattr(job, "jira_id", ""),
            "status": getattr(job, "status", ""),
        }
    data.pop("chat_snapshot", None)
    data.pop("prompts", None)
    text = data.pop("text", "") or ""
    data["result_chars"] = len(str(text))
    err = str(data.get("error_message") or "")
    if len(err) > 500:
        data["error_message"] = err[:500] + "…"
    return data


def _jobs_summary(manager: Any) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "total": 0,
        "by_status": {},
        "by_kind": {},
        "live": [],
        "recent": [],
    }
    try:
        rows = list(manager.store.list_all())
    except Exception:  # noqa: BLE001
        return empty
    by_status: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    live: List[Dict[str, Any]] = []
    for job in rows:
        status = str(getattr(job, "status", "") or "unknown")
        kind = str(getattr(job, "job_kind", "") or "ticket")
        by_status[status] = by_status.get(status, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if getattr(job, "live", False) or status in {"queued", "running"}:
            live.append(
                {
                    "job_id": getattr(job, "job_id", ""),
                    "jira_id": getattr(job, "jira_id", ""),
                    "status": status,
                    "job_kind": kind,
                    "model": getattr(job, "model", "") or "",
                    "session_id": getattr(job, "session_id", "") or "",
                    "accepted_at": getattr(job, "accepted_at", "") or "",
                }
            )
    recent = [_job_summary_row(job) for job in rows[:_MAX_RECENT_JOBS]]
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_kind": by_kind,
        "live": live[:50],
        "recent": recent,
    }


def _path_stats(path: Path, *, sample: int = 16) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "path": str(path) if path else "",
        "exists": False,
        "is_dir": False,
        "file_count": 0,
        "dir_count": 0,
        "bytes": 0,
        "sample": [],
    }
    if not path:
        return info
    root = Path(path)
    try:
        if not root.exists():
            return info
        info["exists"] = True
        info["is_dir"] = root.is_dir()
        if root.is_file():
            info["file_count"] = 1
            info["bytes"] = int(root.stat().st_size)
            info["sample"] = [root.name]
            return info
        files = 0
        dirs = 0
        total = 0
        names: List[str] = []
        for child in root.iterdir():
            try:
                if child.is_dir():
                    dirs += 1
                else:
                    files += 1
                    total += int(child.stat().st_size)
                if len(names) < sample:
                    names.append(child.name)
            except OSError:
                continue
        info["file_count"] = files
        info["dir_count"] = dirs
        info["bytes"] = total
        info["sample"] = names
    except OSError as exc:
        info["error"] = str(exc)
    return info


def _layout(settings: Settings) -> Dict[str, Any]:
    return {
        "data_dir": _path_stats(Path(settings.data_dir)),
        "work_dir": _path_stats(Path(settings.work_dir or "")),
        "job_log_dir": _path_stats(Path(settings.job_log_dir or "")),
        "job_store_dir": _path_stats(Path(settings.job_store_dir or "")),
        "serve_dir": _path_stats(Path(settings.serve_dir or "")),
        "queue_path": _path_stats(Path(settings.queue_path or "")),
        "app_log_path": _path_stats(Path(settings.app_log_path or "")),
        "project_root": _path_stats(Path(settings.project_root or "")),
    }


def _log_file_names(settings: Settings) -> List[str]:
    root = Path(settings.job_log_dir or "")
    if not root.is_dir():
        return []
    try:
        names = sorted(
            p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".log"
        )
    except OSError:
        return []
    return names[:_MAX_LOG_FILE_NAMES]


def _serve_log_rows(settings: Settings) -> List[Dict[str, Any]]:
    serve_dir = settings.serve_dir
    if not serve_dir or not Path(serve_dir).is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        files = [
            p
            for p in Path(serve_dir).iterdir()
            if p.is_file() and p.suffix.lower() == ".log"
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for path in files[:_MAX_SERVE_LOG_NAMES]:
        try:
            st = path.stat()
            rows.append(
                {
                    "name": path.name,
                    "bytes": int(st.st_size),
                    "mtime": int(st.st_mtime),
                }
            )
        except OSError:
            rows.append({"name": path.name, "bytes": 0, "mtime": 0})
    return rows


def _service_logs(settings: Settings) -> List[Dict[str, Any]]:
    root = Path(settings.job_log_dir or "") / "service"
    if not root.is_dir():
        return []
    added: List[Dict[str, Any]] = []
    try:
        files = sorted(
            [p for p in root.iterdir() if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    for path in files[:_MAX_SERVICE_FILES]:
        blob = read_capped_text(path, max_bytes=_MAX_SERVICE_LOG)
        if blob.get("missing"):
            continue
        added.append({"name": path.name, **blob})
    return added



