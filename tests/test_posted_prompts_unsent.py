"""Ticket jobs that never POSTed must not list ORIGINAL."""

import time

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.models import JobRecord, posted_prompt_rows
from opencode_manager.settings import Settings


def test_ticket_job_without_posted_prompts_is_empty() -> None:
    job = JobRecord(
        job_id="job_unsent",
        jira_id="UNSENT-1",
        prompt="this was never sent to OpenCode",
        original_posted=False,
        status="error",
        live=False,
    )
    assert posted_prompt_rows(job) == []


def test_failed_clone_does_not_list_original_as_posted(tmp_settings: Settings) -> None:
    tmp_settings.git_clone_timeout_seconds = 8.0
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post(
            "/jobs",
            json={
                "repo_url": (tmp_settings.work_dir / "missing-origin.git").resolve().as_uri(),
                "prompt": "do work",
                "model": "opencode/hy3-free",
                "agent_mode": "orchestrator",
                "timeout_in_seconds": 30,
                "retry_count": 1,
                "jira_id": "CLONEFAIL-1",
                "callback_url": "",
            },
        )
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        last = None
        for _ in range(80):
            last = client.get(f"/jobs/{job_id}")
            if last.status_code == 200 and last.json().get("live") is False:
                break
            time.sleep(0.1)
        assert last is not None
        assert last.status_code == 200
        assert last.json()["live"] is False
        prompts = client.get(f"/api/jobs/{job_id}/prompts").json()["prompts"]
        assert prompts == []
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        assert job["original_posted"] is False
