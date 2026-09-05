"""GET /api/report-context for the client-built issue zip."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from tests.test_api import FakeRunner


def test_report_context_is_safe_and_includes_logs(tmp_settings: Settings) -> None:
    tmp_settings.app_log_path = tmp_settings.job_log_dir / "app.log"
    tmp_settings.app_log_path.write_text(
        "clone https://oauth2:supersecret@gitlab.example/r.git\nboot ok\n",
        encoding="utf-8",
    )
    (tmp_settings.job_log_dir / "crash.log").write_text("UNCAUGHT boom\n", encoding="utf-8")
    (tmp_settings.job_log_dir / "wrapper-exit.log").write_text("backend exit 1\n", encoding="utf-8")
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        res = client.get("/api/report-context")
        assert res.status_code == 200
        body = res.json()
        dumped = json.dumps(body)
        assert "supersecret" not in dumped
        assert "gitlab.example" in body["app_log"]["text"]
        assert "***" in body["app_log"]["text"]
        assert body["app_log"]["missing"] is False
        assert body["crash_log"]["missing"] is False
        assert "UNCAUGHT boom" in body["crash_log"]["text"]
        assert "backend exit 1" in body["wrapper_exit_log"]["text"]
        assert body["settings"]["listen_host"] == "127.0.0.1"
        assert "callback_url" not in dumped
        assert "PAT" not in dumped
        assert body["runtime"]["osm_version"]
        assert "python" in body["runtime"]
        assert "live" in body
        assert client.post("/api/report-context", json={"note": "x"}).status_code == 405


def test_report_context_wrapper_exit_falls_back_to_project(tmp_settings: Settings) -> None:
    wrapper = tmp_settings.project_root / "logs"
    wrapper.mkdir(parents=True, exist_ok=True)
    (wrapper / "wrapper-exit.log").write_text("legacy wrapper exit\n", encoding="utf-8")
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        body = client.get("/api/report-context").json()
        assert "legacy wrapper exit" in body["wrapper_exit_log"]["text"]


def test_report_context_missing_logs_are_ok(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        body = client.get("/api/report-context").json()
        assert body["wrapper_exit_log"]["missing"] is True
        assert body["queue"]["queued_count"] == 0
        assert isinstance(body["app_log"]["text"], str)
        assert isinstance(body["crash_log"]["text"], str)


def test_report_context_queue_omits_callback_url(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        app.state.manager.queue.enqueue(
            {
                "job_id": "job_queued",
                "jira_id": "Q-WAIT",
                "callback_url": "http://secret-wait.example/wait",
                "source_branch": "main",
                "model": "opencode/x",
                "agent_mode": "orchestrator",
            }
        )
        ctx = client.get("/api/report-context").json()
        items = ctx["queue"]["items"]
        assert any(item.get("job_id") == "job_queued" for item in items)
        dumped = json.dumps(items)
        assert "callback_url" not in dumped
        assert "secret-wait.example" not in dumped
        assert "callback_url" not in ctx["settings"]


def test_job_history_still_has_no_callback_in_record(tmp_settings: Settings) -> None:
    from opencode_manager.dashboard.store import JobStore

    store = JobStore(tmp_settings.job_store_dir)
    store.save(JobRecord(job_id="job_r1", jira_id="R-1", status="error", live=False))
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        job = client.get("/api/jobs/job_r1").json()["job"]
        assert "callback_url" not in job
        assert client.get("/api/report-context").status_code == 200
