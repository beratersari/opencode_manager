from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencode_manager.azure.identity import azure_project_num
from opencode_manager.review_manager import ReviewManager
from opencode_manager.webhook_azure import router as azure_router
from opencode_manager.webhook_gitlab import router as gitlab_router
from tests.review_fakes import FakeRunner
from tests.review_test_azure_events import PROJECT, REPO, _pr


def _pr_with_bot(**extra):
    pr = _pr(**extra)
    pr["reviewers"] = [{"id": "bot-id", "displayName": "creasy"}]
    return pr


def _app(tmp_config, *, azure=True):
    tmp_config.review_mention = tmp_config.review_mention or "creasy"
    if azure:
        tmp_config.azure_url = "https://ado.example/tfs/DefaultCollection"
        tmp_config.azure_token = "pat-test"
        tmp_config.azure_webhook_password = "secret"
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.azure = None
    app.state.azure_bot_user_id = "bot-id"
    app.include_router(gitlab_router)
    app.include_router(azure_router)
    return app, manager, runner


def _auth(password: str = "secret", user: str = "") -> dict[str, str]:
    blob = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {blob}"}


def test_azure_route_does_not_change_gitlab_webhook(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open",
            "iid": 1,
            "target_project_id": 5,
            "source_branch": "f",
            "target_branch": "main",
            "draft": False,
            "title": "Fix login timeout",
            "reviewer_ids": [99],
        },
        "reviewers": [{"id": 99, "username": "creasy"}],
    }
    res = client.post("/amirmini/webhook/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.provider == "gitlab"
    runner.release.set()
    manager.shutdown()


def test_azure_payload_on_gitlab_route_is_ignored(tmp_config):
    app, manager, _runner = _app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/gitlab",
        json={"eventType": "git.pullrequest.created", "resource": _pr()},
        headers={"X-Gitlab-Token": "secret"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_azure_created_accepted(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": _pr_with_bot()},
        headers=_auth(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "accepted"
    job = manager.store.get(body["job_id"])
    assert job is not None
    assert job.provider == "azure"
    assert job.azure_project == PROJECT
    assert job.azure_repo == REPO
    assert job.mr_iid == 12
    assert job.project_id == azure_project_num(PROJECT, REPO)
    assert job.trigger == "open"
    runner.release.set()
    manager.shutdown()


def test_azure_mention_comment_is_accepted(tmp_config):
    tmp_config.review_mention = "creasy"
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={
            "eventType": "git.pullrequest.commented",
            "resource": {
                "comment": {"content": "@creasy /ask check the lock", "author": {"id": "user-1"}},
                "pullRequest": _pr(pullRequestId=14),
            },
        },
        headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.provider == "azure"
    assert job.trigger == "ask"
    assert job.explicit is True
    runner.release.set()
    manager.shutdown()


def test_azure_update_ignored(tmp_config):
    app, manager, _runner = _app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.updated", "resource": _pr()},
        headers=_auth(),
    )
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_gitlab_secret_does_not_lock_azure_route(tmp_config):
    """Dual install: GitLab WEBHOOK_SECRET must not force Azure Basic auth."""
    tmp_config.webhook_secret = "gitlab-only"
    tmp_config.azure_url = "https://ado.example/tfs/DefaultCollection"
    tmp_config.azure_token = "pat-test"
    tmp_config.azure_webhook_password = ""
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.azure = None
    app.state.azure_bot_user_id = "bot-id"
    app.include_router(azure_router)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": _pr_with_bot(pullRequestId=3)},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    runner.release.set()
    manager.shutdown()


def test_azure_secret_required_when_set(tmp_config):
    app, manager, _runner = _app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": _pr()},
    )
    assert res.status_code == 401
    manager.shutdown()


def test_azure_bot_id_is_resolved_before_collection_rebase(tmp_config):
    """Comment classify runs before apply_collection. Host-only URL cannot see /tfs."""
    tmp_config.azure_url = "https://tfs02.company.com.tr"
    tmp_config.azure_token = "pat-test"
    tmp_config.azure_webhook_password = ""
    tmp_config.review_mention = "creasy"
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    order: list[str] = []

    class Azure:
        base_url = "https://tfs02.company.com.tr"

        def current_user_id(self):
            order.append(f"user:{self.base_url}")
            return None

        def apply_collection(self, collection="", web_url=""):
            order.append(f"apply:{web_url}")
            self.base_url = "https://tfs02.company.com.tr/tfs/ExampleCollection"

    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.azure = Azure()
    app.state.azure_bot_user_id = None
    app.include_router(azure_router)
    client = TestClient(app)
    payload = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {"content": "@creasy /ask why", "author": {"id": "bot-id"}},
            "pullRequest": _pr(
                pullRequestId=9,
                url="https://tfs02.company.com.tr/tfs/ExampleCollection/App/_git/app/pullrequest/9",
            ),
        },
    }
    res = client.post("/amirmini/webhook/azure", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    assert order[0].startswith("user:")
    assert order[0] == "user:https://tfs02.company.com.tr"
    assert any(item.startswith("apply:") for item in order)
    assert order.index("user:https://tfs02.company.com.tr") < [
        i for i, item in enumerate(order) if item.startswith("apply:")
    ][0]
    runner.release.set()
    manager.shutdown()


def test_azure_missing_collection_is_400(tmp_config):
    tmp_config.azure_url = "https://tfs02.company.com.tr"
    tmp_config.azure_token = "pat-test"
    tmp_config.azure_webhook_password = ""
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.azure = None
    app.state.azure_bot_user_id = "bot-id"
    app.include_router(azure_router)
    client = TestClient(app)
    pr = _pr_with_bot()
    pr["url"] = "http://ado/pr/12"
    pr["repository"]["remoteUrl"] = ""
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": pr},
    )
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["reason"] == "azure collection missing"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_azure_host_only_config_ok_when_pr_has_collection(tmp_config):
    tmp_config.azure_url = "https://tfs02.company.com.tr"
    tmp_config.azure_token = "pat-test"
    tmp_config.azure_webhook_password = ""
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.azure = None
    app.state.azure_bot_user_id = "bot-id"
    app.include_router(azure_router)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": _pr_with_bot()},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    runner.release.set()
    manager.shutdown()


def test_azure_disabled_is_ignored(tmp_config):
    app, manager, _runner = _app(tmp_config, azure=False)
    client = TestClient(app)
    res = client.post("/amirmini/webhook/azure", json={"eventType": "git.pullrequest.created", "resource": _pr()})
    assert res.status_code == 200
    assert res.json()["reason"] == "azure not configured"
    manager.shutdown()