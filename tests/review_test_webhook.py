from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencode_manager.review_manager import ReviewManager
from opencode_manager.webhook_gitlab import router as webhook_router
from tests.review_fakes import FakeRunner


def _app(tmp_config):
    tmp_config.review_mention = tmp_config.review_mention or "creasy"
    runner = FakeRunner()
    manager = ReviewManager(tmp_config, runner)
    manager.ready = True
    app = FastAPI()
    app.state.config = tmp_config
    app.state.review_manager = manager
    app.state.bot_user_id = 99
    app.include_router(webhook_router)
    return app, manager, runner


def test_ask_runs_when_bot_id_unknown_if_mention_alias_is_set(tmp_config):
    app, manager, runner = _app(tmp_config)
    app.state.bot_user_id = None
    app.state.gitlab = None
    client = TestClient(app)
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask why this lock?"},
        "merge_request": {"iid": 4, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    res = client.post("/amirmini/webhook/gitlab", json=note, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.trigger == "ask"
    runner.release.set()
    manager.shutdown()


def test_note_ignored_when_bot_and_mention_unknown(tmp_config):
    tmp_config.review_mention = ""
    app, manager, runner = _app(tmp_config)
    tmp_config.review_mention = ""
    app.state.bot_user_id = None
    app.state.bot_mention_names = []

    class Gitlab:
        def current_user_id(self):
            return None

        def current_user(self):
            return None

    app.state.gitlab = Gitlab()
    client = TestClient(app)
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask why"},
        "merge_request": {"iid": 4, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    res = client.post("/amirmini/webhook/gitlab", json=note, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert res.json()["reason"] == "bot user unknown"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_secret_required(tmp_config):
    app, _, _ = _app(tmp_config)
    client = TestClient(app)
    res = client.post("/amirmini/webhook/gitlab", json={"object_kind": "merge_request"})
    assert res.status_code == 401


def test_open_accepted(tmp_config):
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
    body = res.json()
    assert body["status"] == "accepted"
    assert "job_id" in body
    job = manager.store.get(body["job_id"])
    assert job is not None
    assert job.mr_title == "Fix login timeout"
    assert job.public_dict()["mr_title"] == "Fix login timeout"
    runner.release.set()
    manager.shutdown()


def test_update_with_new_commits_ignored(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "update",
            "iid": 1,
            "target_project_id": 5,
            "source_branch": "f",
            "target_branch": "main",
            "draft": False,
            "oldrev": "abc123",
            "title": "Fix login timeout",
        },
    }
    res = client.post("/amirmini/webhook/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_command_without_mention_is_ignored(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "/ask"},
        "merge_request": {"iid": 8, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    res = client.post("/amirmini/webhook/gitlab", json=note, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"
    assert manager.store.list_all() == []
    manager.shutdown()


def test_comment_job_keeps_discussion_id(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {
            "noteable_type": "MergeRequest",
            "note": "@creasy /ask why this lock?",
            "discussion_id": "disc_live",
        },
        "merge_request": {"iid": 8, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    res = client.post("/amirmini/webhook/gitlab", json=note, headers={"X-Gitlab-Token": "secret"})
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.discussion_id == "disc_live"
    runner.release.set()
    manager.shutdown()


def test_mention_comment_is_accepted(tmp_config):
    tmp_config.review_mention = "creasy"
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask check the lock"},
        "merge_request": {
            "iid": 8,
            "target_project_id": 5,
            "source_branch": "f",
            "target_branch": "main",
            "title": "Add overflow",
        },
    }
    res = client.post("/amirmini/webhook/gitlab", json=note, headers={"X-Gitlab-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
    job = manager.store.get(res.json()["job_id"])
    assert job is not None
    assert job.trigger == "ask"
    assert job.explicit is True
    runner.release.set()
    manager.shutdown()


def test_comment_queued_while_busy(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    headers = {"X-Gitlab-Token": "secret"}
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask first?"},
        "merge_request": {"iid": 2, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    first = client.post("/amirmini/webhook/gitlab", json=note, headers=headers)
    assert first.json()["status"] == "accepted"
    second = {
        **note,
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask what about errors?"},
    }
    queued = client.post("/amirmini/webhook/gitlab", json=second, headers=headers)
    assert queued.json()["status"] == "queued"
    job = manager.store.get(queued.json()["job_id"])
    assert job is not None
    assert job.trigger == "ask"
    runner.release.set()
    manager.shutdown()


def test_dashboard_cancel_stays_get_only(tmp_config):
    app, manager, runner = _app(tmp_config)
    client = TestClient(app)
    headers = {"X-Gitlab-Token": "secret"}
    note = {
        "object_kind": "note",
        "user": {"id": 1},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask first?"},
        "merge_request": {"iid": 8, "target_project_id": 5, "source_branch": "f", "target_branch": "main"},
    }
    first = client.post("/amirmini/webhook/gitlab", json=note, headers=headers).json()
    second = client.post(
        "/amirmini/webhook/gitlab",
        json={**note, "object_attributes": {"noteable_type": "MergeRequest", "note": "@creasy /ask later?"}},
        headers=headers,
    ).json()
    assert first["status"] in {"accepted", "queued"}
    assert second["status"] == "queued"
    queued = manager.store.get(second["job_id"])
    assert queued is not None
    assert queued.status == "queued"
    assert queued.job_kind == "review"
    runner.release.set()
    manager.shutdown()
