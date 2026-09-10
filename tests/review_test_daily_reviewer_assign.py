"""Daily reviewer-assign: only assigning the bot starts a review."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.review_test_azure_webhook import _app as _azure_app
from tests.review_test_azure_webhook import _auth, _pr_with_bot
from tests.review_test_webhook import _app as _gitlab_app


def test_gitlab_adding_teammate_does_not_start_a_job(tmp_config):
    app, manager, runner = _gitlab_app(tmp_config)
    client = TestClient(app)
    payload = {
        "object_kind": "merge_request",
        "user": {"id": 7, "username": "dev"},
        "object_attributes": {
            "action": "update",
            "iid": 8,
            "target_project_id": 5,
            "source_branch": "f",
            "target_branch": "main",
            "draft": False,
            "title": "Add overflow",
        },
        "changes": {
            "reviewers": {
                "previous": [{"id": 99, "username": "creasy"}],
                "current": [
                    {"id": 99, "username": "creasy"},
                    {"id": 4, "username": "alice"},
                ],
            }
        },
    }
    res = client.post("/amirmini/webhook/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_gitlab_assigning_bot_after_teammate_starts_one_job(tmp_config):
    app, manager, runner = _gitlab_app(tmp_config)
    client = TestClient(app)
    payload = {
        "object_kind": "merge_request",
        "user": {"id": 7, "username": "dev"},
        "object_attributes": {
            "action": "update",
            "iid": 8,
            "target_project_id": 5,
            "source_branch": "f",
            "target_branch": "main",
            "draft": False,
            "title": "Add overflow",
        },
        "changes": {
            "reviewers": {
                "previous": [{"id": 4, "username": "alice"}],
                "current": [
                    {"id": 4, "username": "alice"},
                    {"id": 99, "username": "creasy"},
                ],
            }
        },
    }
    res = client.post("/amirmini/webhook/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"})
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.trigger == "review"
    runner.release.set()
    manager.shutdown()


def test_azure_adding_teammate_does_not_start_a_job(tmp_config):
    app, manager, runner = _azure_app(tmp_config)
    client = TestClient(app)
    pr = _pr_with_bot()
    pr["reviewers"] = [
        {"id": "bot-id", "displayName": "creasy"},
        {"id": "alice", "displayName": "Alice"},
    ]
    res = client.post(
        "/amirmini/webhook/azure",
        json={
            "eventType": "git.pullrequest.updated",
            "notificationType": "ReviewersUpdateNotification",
            "message": {"text": "Jamal Hartnett added Alice as a reviewer"},
            "resource": pr,
        },
        headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_azure_adding_bot_starts_a_job(tmp_config):
    app, manager, runner = _azure_app(tmp_config)
    client = TestClient(app)
    res = client.post(
        "/amirmini/webhook/azure",
        json={
            "eventType": "git.pullrequest.updated",
            "notificationType": "ReviewersUpdateNotification",
            "message": {"text": "Jamal Hartnett added creasy as a reviewer"},
            "resource": _pr_with_bot(pullRequestId=21),
        },
        headers=_auth(),
    )
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.trigger == "review"
    runner.release.set()
    manager.shutdown()


def test_azure_created_then_teammate_add_does_not_queue_second_job(tmp_config):
    app, manager, runner = _azure_app(tmp_config)
    client = TestClient(app)
    created = client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.created", "resource": _pr_with_bot()},
        headers=_auth(),
    )
    assert created.json()["status"] == "accepted"
    pr = _pr_with_bot()
    pr["reviewers"] = list(pr["reviewers"]) + [{"id": "alice", "displayName": "Alice"}]
    later = client.post(
        "/amirmini/webhook/azure",
        json={
            "eventType": "git.pullrequest.updated",
            "notificationType": "ReviewersUpdateNotification",
            "message": {"text": "Dev added Alice as a required reviewer"},
            "resource": pr,
        },
        headers=_auth(),
    )
    assert later.json()["status"] == "ignored"
    assert len(manager.store.list_all()) == 1
    runner.release.set()
    manager.shutdown()
