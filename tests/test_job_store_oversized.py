"""Oversized job JSON must still poll and 409."""

from opencode_manager.dashboard.store import MAX_JSON_SIZE, JobStore
from opencode_manager.manager import Manager
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.settings import Settings


def test_oversized_running_job_still_dedups_and_polls(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    pad = "x" * (MAX_JSON_SIZE + 2048)
    store.save(
        JobRecord(
            job_id="job_huge",
            jira_id="HUGE-1",
            status="running",
            live=True,
            model="opencode/hy3-free",
            agent_mode="orchestrator",
            prompt="do work",
            timeout_in_seconds=30,
            retry_count=1,
            accepted_at=utc_now(),
            chat_snapshot=[
                {
                    "id": "m1",
                    "role": "assistant",
                    "parts": [{"type": "text", "text": pad}],
                }
            ],
        )
    )
    loaded = store.get("job_huge")
    assert loaded is not None
    assert loaded.jira_id == "HUGE-1"
    assert store.live_for_jira("HUGE-1") is not None
    manager = Manager(tmp_settings)
    manager.ready = True
    status, env = manager.submit(
        {
            "repo_url": "https://gitlab.example/g/r.git",
            "prompt": "do work",
            "model": "opencode/hy3-free",
            "agent_mode": "orchestrator",
            "timeout_in_seconds": 30,
            "retry_count": 1,
            "jira_id": "HUGE-1",
            "callback_url": "",
        }
    )
    assert status == 409
    assert env.job_id == "job_huge"
    http, payload = manager.poll_job("job_huge")
    assert http == 202
    assert payload["live"] is True
