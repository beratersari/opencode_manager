"""Regression: list filters paginate the filtered set; queue honors jira_id."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord, job_matches_list_filter, posted_prompt_rows, utc_now
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class FakeRunner:
    def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
        return Terminal(200, "ok")


def test_posted_prompt_rows_falls_back_to_job_prompt() -> None:
    empty = JobRecord(job_id="j0", prompt="")
    assert posted_prompt_rows(empty) == []
    job = JobRecord(job_id="j1", prompt="review this MR", started_at="2026-09-14T00:00:00.000Z")
    rows = posted_prompt_rows(job)
    assert len(rows) == 1
    assert rows[0].id == "ORIGINAL"
    assert rows[0].text == "review this MR"


def test_job_matches_list_filter() -> None:
    running = JobRecord(job_id="j1", jira_id="A", status="running", live=True)
    queued = JobRecord(job_id="j2", jira_id="A", status="queued", live=True)
    err = JobRecord(job_id="j3", jira_id="A", status="error", live=False)
    tout = JobRecord(job_id="j4", jira_id="A", status="timeout", live=False)
    ok = JobRecord(job_id="j5", jira_id="A", status="success", live=False)
    assert job_matches_list_filter(running, "active")
    assert not job_matches_list_filter(queued, "active")
    assert job_matches_list_filter(running, "all")
    assert not job_matches_list_filter(queued, "all")
    assert job_matches_list_filter(err, "error")
    assert job_matches_list_filter(tout, "error")
    assert job_matches_list_filter(ok, "completed")
    assert not job_matches_list_filter(ok, "error")
    review = JobRecord(job_id="j6", jira_id="R", job_kind="review", status="success", live=False)
    usage = JobRecord(
        job_id="j7",
        jira_id="R",
        job_kind="review",
        trigger="usage",
        status="success",
        live=False,
    )
    assert job_matches_list_filter(review, "review")
    assert not job_matches_list_filter(ok, "review")
    assert not job_matches_list_filter(usage, "review")


def test_jobs_filter_paginates_filtered_set(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    for i in range(30):
        store.save(
            JobRecord(
                job_id=f"job_err_{i:02d}",
                jira_id=f"E-{i}",
                status="error",
                live=False,
                accepted_at=utc_now(),
            )
        )
    for i in range(5):
        store.save(
            JobRecord(
                job_id=f"job_ok_{i:02d}",
                jira_id=f"S-{i}",
                status="success",
                live=False,
                accepted_at=utc_now(),
            )
        )
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        page1 = client.get("/api/jobs", params={"filter": "error", "page": 1, "page_size": 25})
        assert page1.status_code == 200
        body = page1.json()
        assert body["total"] == 30
        assert body["filter"] == "error"
        assert len(body["jobs"]) == 25
        assert all(j["status"] == "error" for j in body["jobs"])
        page2 = client.get("/api/jobs", params={"filter": "error", "page": 2, "page_size": 25})
        assert page2.json()["total"] == 30
        assert len(page2.json()["jobs"]) == 5
        done = client.get("/api/jobs", params={"filter": "completed", "page_size": 25})
        assert done.json()["total"] == 5
        assert all(j["status"] == "success" for j in done.json()["jobs"])


def test_review_filter_via_api(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_ticket",
            jira_id="T-1",
            status="success",
            live=False,
            accepted_at=utc_now(),
        )
    )
    store.save(
        JobRecord(
            job_id="job_review",
            jira_id="42-7",
            job_kind="review",
            source="group/app",
            provider="gitlab",
            status="success",
            live=False,
            accepted_at=utc_now(),
        )
    )
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        body = client.get("/api/jobs", params={"filter": "review"}).json()
        assert body["total"] == 1
        assert body["jobs"][0]["job_id"] == "job_review"
        assert body["jobs"][0]["source"] == "group/app"


def test_usage_jobs_are_hidden_from_dashboard(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_usage",
            jira_id="42-7",
            job_kind="review",
            trigger="usage",
            status="success",
            live=False,
            accepted_at=utc_now(),
        )
    )
    store.save(
        JobRecord(
            job_id="job_ask",
            jira_id="42-7",
            job_kind="review",
            trigger="ask",
            status="success",
            live=False,
            accepted_at=utc_now(),
        )
    )
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        listed = client.get("/api/jobs").json()
        ids = [j["job_id"] for j in listed["jobs"]]
        assert "job_ask" in ids
        assert "job_usage" not in ids
        assert client.get("/api/jobs/job_usage").status_code == 404
        assert client.get("/api/jobs/job_ask").status_code == 200


def test_review_job_prompt_tab_uses_stored_prompt(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_rev",
            jira_id="42-7",
            job_kind="review",
            trigger="review",
            status="success",
            live=False,
            prompt="Review this merge request.",
            accepted_at=utc_now(),
        )
    )
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        body = client.get("/api/jobs/job_rev/prompts").json()
        assert len(body["prompts"]) == 1
        assert body["prompts"][0]["id"] == "ORIGINAL"
        assert "merge request" in body["prompts"][0]["text"]


def test_queue_jira_filter(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        manager = app.state.manager
        manager.queue.enqueue(
            {
                "job_id": "job_a",
                "jira_id": "AA-1",

                "accepted_at": utc_now(),
                "model": "opencode/x",
                "agent_mode": "build",
            }
        )
        manager.queue.enqueue(
            {
                "job_id": "job_b",
                "jira_id": "BB-1",

                "accepted_at": utc_now(),
                "model": "opencode/x",
                "agent_mode": "build",
            }
        )
        all_q = client.get("/api/queue").json()
        assert all_q["queued_count"] == 2
        only_a = client.get("/api/queue", params={"jira_id": "AA-1"}).json()
        assert only_a["queued_count"] == 1
        assert only_a["items"][0]["jira_id"] == "AA-1"
        dumped = str(only_a)
        assert "secret" not in dumped
