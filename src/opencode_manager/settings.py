"""Load operator settings from a YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\osm")
    return Path("/var/lib/osm")


@dataclass
class Settings:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    max_concurrent_jobs: int = 2
    callback_timeout_seconds: float = 15.0
    callback_retry_count: int = 3
    callback_allowed_hosts: List[str] = field(default_factory=list)
    data_dir: Path = field(default_factory=_default_data_dir)
    work_dir: Optional[Path] = None
    job_log_dir: Optional[Path] = None
    job_store_dir: Optional[Path] = None
    queue_path: Optional[Path] = None
    serve_dir: Optional[Path] = None
    app_log_path: Optional[Path] = None
    log_level: str = "INFO"
    opencode_bin: str = "opencode"
    hang_timeout_seconds: float = 180.0
    git_clone_timeout_seconds: float = 300.0
    retry_backoff_seconds: float = 2.0
    retry_backoff_cap_seconds: float = 30.0
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)

    def __post_init__(self) -> None:
        self.apply_layout()

    def apply_layout(self) -> None:
        """Fill derived paths. YAML load sets them from data_dir; tests may override."""
        root = Path(self.data_dir)
        derived = self.work_dir is None
        if self.work_dir is None:
            self.work_dir = root / ".temp"
        if self.job_log_dir is None:
            self.job_log_dir = root / "logs"
        if self.job_store_dir is None:
            self.job_store_dir = root / "jobs"
        if self.queue_path is None:
            self.queue_path = root / "queue.json"
        if self.serve_dir is None:
            self.serve_dir = root / ".serve" if derived else Path(self.work_dir) / ".serve"
        if self.app_log_path is None:
            self.app_log_path = Path(self.job_log_dir) / "app.log"

    def ensure_dirs(self) -> None:
        self.apply_layout()
        assert self.work_dir and self.job_log_dir and self.job_store_dir
        assert self.queue_path and self.serve_dir and self.app_log_path
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.job_log_dir.mkdir(parents=True, exist_ok=True)
        self.job_store_dir.mkdir(parents=True, exist_ok=True)
        self.serve_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.app_log_path.parent.mkdir(parents=True, exist_ok=True)


def _as_path(value: Any, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(str(value))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must be a mapping")
    return loaded


def load_settings(path: Optional[Path] = None) -> Settings:
    settings_path = path or Path(os.environ.get("OSM_SETTINGS", _PROJECT_ROOT / "settings.yaml"))
    data = _read_yaml(settings_path)
    # Local machine overrides. Not used when tests pass an explicit path.
    if path is None and not os.environ.get("OSM_SETTINGS"):
        data = {**data, **_read_yaml(_PROJECT_ROOT / "settings.local.yaml")}
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
    s.data_dir = _as_path(data.get("data_dir"), s.data_dir)
    s.work_dir = s.data_dir / ".temp"
    s.job_log_dir = s.data_dir / "logs"
    s.job_store_dir = s.data_dir / "jobs"
    s.queue_path = s.data_dir / "queue.json"
    s.serve_dir = s.data_dir / ".serve"
    s.app_log_path = s.job_log_dir / "app.log"
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
