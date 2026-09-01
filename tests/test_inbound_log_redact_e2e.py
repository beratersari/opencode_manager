"""Real POST /jobs: PAT / userinfo must not land in app.log."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.settings import Settings

_AZURE = "AZURE_E2E_PAT_TOKEN_9f3c"
_GITLAB = "GITLAB_E2E_OAUTH_TOKEN_7a1b"
_COLON = "COLON_E2E_PAT_TOKEN_2c8a"


def _body(repo_url: str, **overrides) -> dict:
    data = {
        "repo_url": repo_url,
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "build",
        "timeout_in_seconds": 5,
        "retry_count": 1,
        "jira_id": "LOG-1",
        "callback_url": "http://127.0.0.1:9/wait",
    }
    data.update(overrides)
    return data


def _read_app_log(settings: Settings) -> str:
    path = settings.app_log_path
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_azure_username_pat_url_is_redacted_in_app_log(tmp_settings: Settings) -> None:
    tmp_settings.git_clone_timeout_seconds = 3.0
    tmp_settings.callback_timeout_seconds = 1.0
    tmp_settings.callback_retry_count = 1
    repo = f"https://{_AZURE}@127.0.0.1:1/org/proj/_git/repo"
    with TestClient(create_app(tmp_settings)) as client:
        res = client.post("/jobs", json=_body(repo, jira_id="LOG-AZURE"))
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        dumped = str(detail)
        assert _AZURE not in dumped
    text = _read_app_log(tmp_settings)
    assert "inbound POST /jobs" in text
    assert _AZURE not in text
    assert "https://***@127.0.0.1:1/" in text


def test_gitlab_oauth2_and_colon_userinfo_redacted_in_app_log(tmp_settings: Settings) -> None:
    tmp_settings.git_clone_timeout_seconds = 3.0
    tmp_settings.callback_timeout_seconds = 1.0
    tmp_settings.callback_retry_count = 1
    with TestClient(create_app(tmp_settings)) as client:
        gitlab = client.post(
            "/jobs",
            json=_body(
                f"https://oauth2:{_GITLAB}@127.0.0.1:1/g/r.git",
                jira_id="LOG-GL",
            ),
        )
        assert gitlab.status_code == 202
        colon = client.post(
            "/jobs",
            json=_body(
                f"https://:{_COLON}@127.0.0.1:1/org/proj/_git/repo",
                jira_id="LOG-COLON",
            ),
        )
        assert colon.status_code == 202
    text = _read_app_log(tmp_settings)
    assert _GITLAB not in text
    assert _COLON not in text
    assert "https://***:***@127.0.0.1:1/" in text
    assert "https://:***@127.0.0.1:1/" in text
