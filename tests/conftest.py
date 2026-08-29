from __future__ import annotations

from pathlib import Path

import pytest

from opencode_manager.settings import Settings


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=2,
        callback_timeout_seconds=2.0,
        callback_retry_count=2,
        work_dir=tmp_path / "work",
        job_log_dir=tmp_path / "joblogs",
        job_store_dir=tmp_path / "jobs",
        queue_path=tmp_path / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=30.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp_path,
    )
    settings.ensure_dirs()
    return settings
