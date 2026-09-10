from __future__ import annotations

import threading
from pathlib import Path

import pytest

from opencode_manager.review_config import ReviewConfig
from opencode_manager.review_worker import RunResult
from opencode_manager.settings import Settings


class FakeRunner:
    """Review-path fake. n8n tests define their own runner locally."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs: list[str] = []
        self.current: str = ""

    def run(self, job, should_stop):  # noqa: ANN001
        self.current = job.job_id
        self.runs.append(job.trigger + ":" + (job.comment_text or ""))
        self.started.set()
        while not self.release.wait(0.05):
            if should_stop():
                return RunResult(cancelled=True, error="cancelled")
        self.release.clear()
        return RunResult(text="ok " + job.trigger, session_id="ses_test", posted=True)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=2,
        callback_timeout_seconds=2.0,
        callback_retry_count=2,
        data_dir=tmp_path / "data",
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


@pytest.fixture
def tmp_config(tmp_path: Path) -> ReviewConfig:
    cfg = ReviewConfig(
        data_dir=tmp_path / "data",
        webhook_secret="secret",
        gitlab_token="token",
        max_concurrent_jobs=2,
        skip_draft_mrs=True,
        work_dir=tmp_path / "data" / "workspaces",
        job_dir=tmp_path / "data" / "jobs",
        log_dir=tmp_path / "data" / "logs",
        serve_dir=tmp_path / "data" / "serve",
    )
    cfg.ensure_dirs()
    return cfg
