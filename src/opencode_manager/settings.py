"""Load operator settings from a YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_work_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\osm\.temp")
    return Path("/var/lib/osm/.temp")


def _default_job_log_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\osm\logs")
    return Path("/var/lib/osm/logs")


def _default_job_store_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\osm\jobs")
    return Path("/var/lib/osm/jobs")


@dataclass
class Settings:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    max_concurrent_jobs: int = 2
    callback_timeout_seconds: float = 15.0
    callback_retry_count: int = 3
    callback_allowed_hosts: List[str] = field(default_factory=list)
    work_dir: Path = field(default_factory=_default_work_dir)
    job_log_dir: Path = field(default_factory=_default_job_log_dir)
    job_store_dir: Path = field(default_factory=_default_job_store_dir)
    queue_path: Path = field(default_factory=lambda: _default_work_dir() / "queue.json")
    log_level: str = "INFO"
    opencode_bin: str = "opencode"
    hang_timeout_seconds: float = 180.0
    git_clone_timeout_seconds: float = 300.0
    retry_backoff_seconds: float = 2.0
    retry_backoff_cap_seconds: float = 30.0
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.job_log_dir.mkdir(parents=True, exist_ok=True)
        self.job_store_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        (self.project_root / "logs").mkdir(parents=True, exist_ok=True)


def _as_path(value: Any, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(str(value))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def load_settings(path: Optional[Path] = None) -> Settings:
    settings_path = path or Path(os.environ.get("OSM_SETTINGS", _PROJECT_ROOT / "settings.yaml"))
    data: dict[str, Any] = {}
    if settings_path.is_file():
        loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("settings file must be a mapping")
        data = loaded
    s = Settings()
    s.listen_host = str(data.get("listen_host", s.listen_host))
    s.listen_port = int(data.get("listen_port", s.listen_port))
    s.max_concurrent_jobs = int(data.get("max_concurrent_jobs", s.max_concurrent_jobs))
    s.callback_timeout_seconds = float(
        data.get("callback_timeout_seconds", s.callback_timeout_seconds)
    )
    s.callback_retry_count = int(data.get("callback_retry_count", s.callback_retry_count))
    hosts = data.get("callback_allowed_hosts") or []
    s.callback_allowed_hosts = [str(h).strip().lower() for h in hosts if str(h).strip()]
    s.work_dir = _as_path(data.get("work_dir"), s.work_dir)
    s.job_log_dir = _as_path(data.get("job_log_dir"), s.job_log_dir)
    s.job_store_dir = _as_path(data.get("job_store_dir"), s.job_store_dir)
    s.queue_path = _as_path(data.get("queue_path"), s.work_dir / "queue.json")
    s.log_level = str(data.get("log_level", s.log_level)).upper()
    s.opencode_bin = str(data.get("opencode_bin", s.opencode_bin))
    s.hang_timeout_seconds = float(data.get("hang_timeout_seconds", s.hang_timeout_seconds))
    s.git_clone_timeout_seconds = float(
        data.get("git_clone_timeout_seconds", s.git_clone_timeout_seconds)
    )
    s.retry_backoff_seconds = float(data.get("retry_backoff_seconds", s.retry_backoff_seconds))
    s.retry_backoff_cap_seconds = float(
        data.get("retry_backoff_cap_seconds", s.retry_backoff_cap_seconds)
    )
    return s
