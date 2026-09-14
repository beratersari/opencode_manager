"""Queued jobs must persist the public repo URL."""

from pathlib import Path

from opencode_manager.manager import Manager
from opencode_manager.settings import Settings


def test_queued_job_disk_has_no_repo_userinfo(tmp_settings: Settings) -> None:
    tmp_settings.max_concurrent_jobs = 1
    manager = Manager(tmp_settings)
    manager.ready = True
    manager._running = 1
    status, env = manager.submit(
        {
            "repo_url": "https://oauth2:s3cretPAT99@gitlab.example/g/r.git",
            "prompt": "do work",
            "model": "opencode/hy3-free",
            "agent_mode": "orchestrator",
            "timeout_in_seconds": 30,
            "retry_count": 1,
            "jira_id": "Q-USERINFO",
            "callback_url": "",
        }
    )
    assert status == 202
    assert env.job_id
    blob = Path(tmp_settings.queue_path).read_text(encoding="utf-8")
    assert "s3cretPAT99" not in blob
    assert "oauth2:" not in blob
    row = manager.store.get(env.job_id)
    assert row is not None
    assert "s3cretPAT99" not in row.repo_url
    assert row.repo_url == "https://gitlab.example/g/r.git"
