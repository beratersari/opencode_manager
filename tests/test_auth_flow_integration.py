"""HTTP integration: dashboard cookie, n8n Bearer, webhooks stay separate."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from opencode_manager.app import create_app
from opencode_manager.dashboard.auth import SESSION_COOKIE
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class N8nRunner:
    def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
        return Terminal(200, "ok")


_JOB = {
    "repo_url": "https://example.com/repo.git",
    "prompt": "do the work",
    "model": "opencode/big-pickle",
    "agent_mode": "orchestrator",
    "timeout_in_seconds": 30,
    "retry_count": 1,
    "jira_id": "AUTH-FLOW-1",
}

_DASHBOARD_GETS = (
    "/api/meta",
    "/api/jobs",
    "/api/queue",
    "/api/report-context",
)


def _app(tmp_settings: Settings, **fields: object) -> TestClient:
    for key, value in fields.items():
        setattr(tmp_settings, key, value)
    return TestClient(create_app(tmp_settings, runner=N8nRunner()))


def test_auth_off_keeps_n8n_and_dashboard_open(tmp_settings: Settings) -> None:
    with _app(tmp_settings) as client:
        assert client.get("/api/auth").json() == {
            "required": False,
            "authenticated": True,
            "has_username": False,
        }
        assert client.get("/api/jobs").status_code == 200
        assert client.post("/jobs", json=_JOB).status_code == 202
        deleted = client.request(
            "DELETE",
            "/sessions",
            content=json.dumps({"jira_id": "AUTH-FLOW-1", "session_id": "ses_missing"}),
            headers={"Content-Type": "application/json"},
        )
        assert deleted.status_code in {200, 404, 409, 500}


def test_dashboard_get_routes_need_login_then_cookie(tmp_settings: Settings) -> None:
    with _app(tmp_settings, dashboard_user="berat", dashboard_password="s3cret") as client:
        assert client.get("/api/auth").status_code == 200
        assert client.get("/api/auth").json()["required"] is True
        for path in _DASHBOARD_GETS:
            assert client.get(path).status_code == 401, path
        assert client.post("/api/login", json={"username": "berat", "password": "s3cret"}).status_code == 200
        for path in _DASHBOARD_GETS:
            assert client.get(path).status_code == 200, path
        assert client.post("/api/jobs").status_code == 405


def test_login_with_token_as_password_when_no_username(tmp_settings: Settings) -> None:
    with _app(tmp_settings, dashboard_token="only-token") as client:
        bad = client.post("/api/login", json={"username": "", "password": "nope"})
        assert bad.status_code == 401
        ok = client.post("/api/login", json={"username": "", "password": "only-token"})
        assert ok.status_code == 200
        assert client.get("/api/jobs").status_code == 200


def test_n8n_post_poll_delete_need_bearer(tmp_settings: Settings) -> None:
    with _app(tmp_settings, dashboard_token="n8n-secret") as client:
        headers = {"Authorization": "Bearer n8n-secret"}
        assert client.post("/jobs", json=_JOB).status_code == 401
        assert client.request(
            "DELETE",
            "/sessions",
            content=json.dumps({"jira_id": "X", "session_id": "ses_x"}),
            headers={"Content-Type": "application/json"},
        ).status_code == 401
        posted = client.post("/jobs", headers=headers, json=_JOB)
        assert posted.status_code == 202
        job_id = posted.json()["job_id"]
        assert client.get(f"/jobs/{job_id}").status_code == 401
        poll = client.get(f"/jobs/{job_id}", headers=headers)
        assert poll.status_code in {200, 202}
        assert "status_code" in poll.json()
        assert client.get(f"/jobs/{job_id}", headers={"Authorization": "Bearer wrong"}).status_code == 401
        unknown = client.get("/jobs/job_does_not_exist", headers=headers)
        assert unknown.status_code == 404
        deleted = client.request(
            "DELETE",
            "/sessions",
            content=json.dumps({"jira_id": "AUTH-FLOW-1", "session_id": "ses_missing"}),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert deleted.status_code in {200, 404, 409, 500}


def test_password_only_does_not_accept_random_bearer(tmp_settings: Settings) -> None:
    with _app(tmp_settings, dashboard_user="berat", dashboard_password="s3cret") as client:
        assert client.post("/jobs", headers={"Authorization": "Bearer s3cret"}, json=_JOB).status_code == 401
        assert client.post("/api/login", json={"username": "berat", "password": "s3cret"}).status_code == 200
        assert client.post("/jobs", json=_JOB).status_code == 202


def test_dashboard_token_does_not_replace_webhook_secret(tmp_settings: Settings) -> None:
    with _app(
        tmp_settings,
        dashboard_token="n8n-secret",
        webhook_secret="hook-secret",
    ) as client:
        assert client.post(
            "/amirmini/webhook/gitlab",
            headers={"Authorization": "Bearer n8n-secret"},
            json={"object_kind": "note"},
        ).status_code == 401
        res = client.post(
            "/amirmini/webhook/gitlab",
            headers={"X-Gitlab-Token": "hook-secret", "Authorization": "Bearer n8n-secret"},
            json={"object_kind": "note", "object_attributes": {"noteable_type": "MergeRequest", "note": "hi"}},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "ignored"


def test_websocket_rejects_unauthenticated(tmp_settings: Settings) -> None:
    tmp_settings.dashboard_token = "n8n-secret"
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws"):
                raise AssertionError("unauthenticated /ws must not stay open")
        except WebSocketDisconnect as exc:
            assert getattr(exc, "code", 4401) in {4401, 1000, 1006, 1008}
        with client.websocket_connect("/ws", headers={"Authorization": "Bearer n8n-secret"}) as ws:
            data = ws.receive_json()
            assert "running" in data
            assert "queue_queued" in data
