"""Dashboard login uses settings user/password and an httpOnly session cookie."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.auth import (
    SESSION_COOKIE,
    credentials_ok,
    dashboard_auth_required,
    make_session,
    session_ok,
)
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class N8nRunner:
    def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
        return Terminal(200, "ok")


def test_credentials_require_user_and_password() -> None:
    settings = Settings(dashboard_user="berat", dashboard_password="s3cret")
    assert dashboard_auth_required(settings)
    assert credentials_ok(settings, "berat", "s3cret")
    assert not credentials_ok(settings, "other", "s3cret")
    assert not credentials_ok(settings, "berat", "wrong")
    assert not credentials_ok(settings, "berat", "")


def test_legacy_token_is_accepted_as_password() -> None:
    settings = Settings(dashboard_token="dash-secret")
    assert credentials_ok(settings, "", "dash-secret")
    assert not credentials_ok(settings, "", "nope")


def test_session_roundtrip_and_expiry() -> None:
    settings = Settings(dashboard_password="s3cret")
    issued = 1_700_000_000
    cookie = make_session(settings, now=issued)
    assert session_ok(settings, cookie, now=issued + 60)
    assert not session_ok(settings, cookie, now=issued + 13 * 60 * 60)
    assert not session_ok(settings, "v1.nope.sig", now=issued)


def test_login_sets_httponly_cookie_and_unlocks_jobs(tmp_settings: Settings) -> None:
    tmp_settings.dashboard_user = "berat"
    tmp_settings.dashboard_password = "s3cret"
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        assert client.get("/api/jobs").status_code == 401
        denied = client.post("/api/login", json={"username": "berat", "password": "wrong"})
        assert denied.status_code == 401
        ok = client.post("/api/login", json={"username": "berat", "password": "s3cret"})
        assert ok.status_code == 200
        cookie = ok.cookies.get(SESSION_COOKIE)
        assert cookie
        header = ok.headers.get("set-cookie") or ""
        assert "HttpOnly" in header or "httponly" in header.lower()
        assert client.get("/api/jobs").status_code == 200
        auth = client.get("/api/auth").json()
        assert auth["required"] is True
        assert auth["authenticated"] is True
        assert auth["has_username"] is True
        client.post("/api/logout")
        assert client.get("/api/jobs").status_code == 401


def test_n8n_bearer_token_unlocks_jobs_and_poller(tmp_settings: Settings) -> None:
    tmp_settings.dashboard_token = "n8n-secret"
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        denied = client.post(
            "/jobs",
            json={
                "repo_url": "https://example.com/repo.git",
                "prompt": "do the work",
                "model": "opencode/big-pickle",
                "agent_mode": "orchestrator",
                "timeout_in_seconds": 30,
                "retry_count": 1,
                "jira_id": "AUTH-1",
            },
        )
        assert denied.status_code == 401
        ok = client.post(
            "/jobs",
            headers={"Authorization": "Bearer n8n-secret"},
            json={
                "repo_url": "https://example.com/repo.git",
                "prompt": "do the work",
                "model": "opencode/big-pickle",
                "agent_mode": "orchestrator",
                "timeout_in_seconds": 30,
                "retry_count": 1,
                "jira_id": "AUTH-1",
            },
        )
        assert ok.status_code == 202
        job_id = ok.json()["job_id"]
        poll = client.get(f"/jobs/{job_id}", headers={"X-Amir-Mini-Token": "n8n-secret"})
        assert poll.status_code in {200, 202}
        creasy = client.get(f"/jobs/{job_id}", headers={"X-Creasy-Token": "n8n-secret"})
        assert creasy.status_code in {200, 202}


def test_webhook_does_not_use_dashboard_token(tmp_settings: Settings) -> None:
    tmp_settings.dashboard_token = "n8n-secret"
    tmp_settings.webhook_secret = "hook-secret"
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        denied = client.post("/amirmini/webhook/gitlab", json={"object_kind": "note"})
        assert denied.status_code == 401
        ignored = client.post(
            "/amirmini/webhook/gitlab",
            headers={"X-Gitlab-Token": "hook-secret"},
            json={"object_kind": "note", "object_attributes": {"noteable_type": "MergeRequest", "note": "hi"}},
        )
        assert ignored.status_code == 200
        assert ignored.json()["status"] == "ignored"
