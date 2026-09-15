from __future__ import annotations

from opencode_manager.gitlab.events import ReviewTrigger, classify_webhook
from opencode_manager.gitlab.urls import gitlab_api_root, gitlab_http_url
from opencode_manager.settings import Settings, load_settings


def test_gitlab_api_root_strips_project_and_mr() -> None:
    assert (
        gitlab_api_root("https://gitlab.example/group/repo.git", "group/repo")
        == "https://gitlab.example"
    )
    assert (
        gitlab_api_root("https://gitlab.example/group/repo/-/merge_requests/9", "group/repo")
        == "https://gitlab.example"
    )
    assert (
        gitlab_api_root("https://host/gitlab/group/repo", "group/repo")
        == "https://host/gitlab"
    )


def test_gitlab_classify_keeps_clone_url() -> None:
    payload = {
        "object_kind": "merge_request",
        "project": {
            "id": 7,
            "path_with_namespace": "group/repo",
            "http_url_to_repo": "https://gitlab.example/group/repo.git",
        },
        "object_attributes": {
            "iid": 3,
            "action": "open",
            "source_branch": "feat",
            "target_branch": "main",
            "url": "https://gitlab.example/group/repo/-/merge_requests/3",
            "title": "feat",
            "last_commit": {"id": "abc"},
        },
        "reviewers": [{"username": "bot", "id": 1}],
    }
    got = classify_webhook(payload, skip_drafts=False, bot_user_id=1, mention_names=["bot"])
    assert isinstance(got, ReviewTrigger)
    assert got.http_url == "https://gitlab.example/group/repo.git"
    assert got.web_url.endswith("/merge_requests/3")


def test_gitlab_http_url_from_project() -> None:
    payload = {
        "project": {
            "path_with_namespace": "group/repo",
            "http_url_to_repo": "https://gitlab.example/group/repo.git",
            "web_url": "https://gitlab.example/group/repo",
        }
    }
    assert gitlab_http_url(payload) == "https://gitlab.example/group/repo.git"


def test_leftover_webhook_secret_loads_as_gitlab_webhook_secret(tmp_path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("webhook_secret: leftover-hook\n", encoding="utf-8")
    got = load_settings(path)
    assert got.gitlab_webhook_secret == "leftover-hook"
    assert not hasattr(got, "gitlab_url")
    assert not hasattr(got, "gitlab_token")
    assert not hasattr(got, "azure_url")
    assert not hasattr(got, "azure_token")
    assert Settings().gitlab_webhook_secret == ""
