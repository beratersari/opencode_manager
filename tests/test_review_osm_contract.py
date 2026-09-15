"""Review is additive. n8n planner/orchestrator contracts stay the same."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord, job_matches_list_filter, utc_now
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class N8nRunner:
    def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
        return Terminal(200, "n8n-ok")


class FakeReview:
    def run(self, job, should_stop):  # noqa: ANN001, ARG002
        from opencode_manager.review_worker import RunResult

        return RunResult(text="review-ok", session_id="ses_test", posted=True)


def _open_with_reviewer() -> dict:
    return {
        "object_kind": "merge_request",
        "project": {"id": 42, "path_with_namespace": "group/app"},
        "object_attributes": {
            "action": "open",
            "iid": 7,
            "target_project_id": 42,
            "source_branch": "feat",
            "target_branch": "main",
            "url": "http://gl/group/app/-/merge_requests/7",
            "title": "Fix login",
            "draft": False,
        },
        "reviewers": [{"id": 99, "username": "amir"}],
    }


def test_review_filter_matches_only_review_jobs() -> None:
    ticket = JobRecord(job_id="j1", jira_id="AA-1", status="success", live=False)
    review = JobRecord(
        job_id="j2",
        jira_id="42-7",
        job_kind="review",
        source="group/app",
        status="success",
        live=False,
    )
    assert not job_matches_list_filter(ticket, "review")
    assert job_matches_list_filter(review, "review")
    assert job_matches_list_filter(review, "completed")
    assert job_matches_list_filter(ticket, "completed")


def test_live_for_jira_ignores_review_jobs(tmp_settings: Settings) -> None:
    store = JobStore(tmp_settings.job_store_dir)
    store.save(
        JobRecord(
            job_id="job_rev",
            jira_id="AA-1",
            job_kind="review",
            status="running",
            live=True,
            accepted_at=utc_now(),
        )
    )
    assert store.live_for_jira("AA-1") is None
    store.save(
        JobRecord(
            job_id="job_n8n",
            jira_id="AA-1",
            job_kind="ticket",
            status="running",
            live=True,
            accepted_at=utc_now(),
        )
    )
    live = store.live_for_jira("AA-1")
    assert live is not None
    assert live.job_id == "job_n8n"


def test_n8n_post_jobs_unchanged_with_review_wired(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        res = client.post(
            "/jobs",
            json={
                "repo_url": "https://example.com/repo.git",
                "prompt": "do the work",
                "model": "opencode/big-pickle",
                "agent_mode": "orchestrator",
                "timeout_in_seconds": 30,
                "retry_count": 1,
                "jira_id": "AA-99",
            },
        )
        assert res.status_code == 202
        body = res.json()
        assert body["jira_id"] == "AA-99"
        assert body["job_id"]
        assert body["status_code"] == 202
        unknown = client.post("/jobs", json={"jira_id": "AA-1", "agent_mode": "code-reviewer"})
        assert unknown.status_code == 400


def test_review_webhooks_and_source_on_list(tmp_settings: Settings) -> None:
    tmp_settings.gitlab_webhook_secret = "secret"
    tmp_settings.review_mention = "amir"
    app = create_app(tmp_settings, runner=N8nRunner(), review_runner=FakeReview)
    with TestClient(app) as client:
        app.state.bot_user_id = 99
        app.state.bot_mention_names = ["amir"]
        denied = client.post("/amirmini/webhook/gitlab", json=_open_with_reviewer())
        assert denied.status_code == 401
        accepted = client.post(
            "/amirmini/webhook/gitlab",
            json=_open_with_reviewer(),
            headers={"X-Gitlab-Token": "secret"},
        )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["status"] in {"accepted", "queued"}
        assert body["job_id"]
        listed = client.get("/api/jobs", params={"filter": "review"})
        assert listed.status_code == 200
        rows = listed.json()["jobs"]
        assert listed.json()["total"] >= 1
        assert any(j["job_kind"] == "review" for j in rows)
        assert any(j.get("source") == "group/app" for j in rows)
        write = client.post("/api/jobs")
        assert write.status_code == 405
        azure = client.post("/amirmini/webhook/azure", json={"eventType": "git.pullrequest.created"})
        assert azure.status_code == 200
        assert azure.json()["status"] == "ignored"


def test_public_dict_includes_review_source() -> None:
    job = JobRecord(
        job_id="job_x",
        jira_id="42-7",
        job_kind="review",
        source="group/app",
        provider="gitlab",
        web_url="http://gl/mr/7",
        mr_title="Fix login",
        trigger="open",
        agent="code-reviewer",
    )
    data = job.public_dict()
    assert data["source"] == "group/app"
    assert data["job_kind"] == "review"
    assert data["provider"] == "gitlab"
    assert data["agent_mode"] == "code-reviewer"
    assert "prompt" not in data
    assert "callback_url" not in data
