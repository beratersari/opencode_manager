"""GET /api/jobs must not ship chat_snapshot."""

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord, utc_now
from opencode_manager.settings import Settings


def test_jobs_list_omits_chat_snapshot(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_snap",
            jira_id="SNAP-1",
            status="success",
            live=False,
            accepted_at=utc_now(),
            chat_snapshot=[
                {
                    "id": "m1",
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "transcript"}],
                }
            ],
        )
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        body = client.get("/api/jobs").json()
        assert body["total"] == 1
        assert "chat_snapshot" not in body["jobs"][0]
        detail = client.get("/api/jobs/job_snap").json()["job"]
        assert detail["chat_snapshot"][0]["id"] == "m1"
