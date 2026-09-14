from __future__ import annotations

import pytest

from opencode_manager.azure.events import classify_azure_webhook, reset_reviewer_cache
from opencode_manager.azure.identity import azure_mr_key, azure_project_num
from opencode_manager.gitlab.events import CleanupTrigger, Ignore, ReviewTrigger


@pytest.fixture(autouse=True)
def _clear_reviewer_cache() -> None:
    reset_reviewer_cache()
    yield
    reset_reviewer_cache()


PROJECT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REPO = "11111111-2222-3333-4444-555555555555"


def _pr(**extra):
    data = {
        "pullRequestId": 12,
        "title": "Add overflow",
        "isDraft": False,
        "status": "active",
        "sourceRefName": "refs/heads/feat",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "abc123"},
        "url": "http://ado/pr/12",
        "repository": {
            "id": REPO,
            "name": "app",
            "project": {"id": PROJECT, "name": "App"},
            "remoteUrl": "https://ado.example/tfs/DefaultCollection/App/_git/app",
        },
    }
    data.update(extra)
    return data


def test_pr_created_without_reviewer_is_ignored():
    got = classify_azure_webhook(
        {"eventType": "git.pullrequest.created", "resource": _pr()},
        bot_user_id="bot-guid",
        mention_names=["Creasy"],
    )
    assert isinstance(got, Ignore)
    assert got.reason == "reviewer not assigned"


def test_pr_created_enqueues_open():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    got = classify_azure_webhook(
        {"eventType": "git.pullrequest.created", "resource": pr},
        bot_user_id="bot-guid",
        mention_names=["Creasy"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "open"
    assert got.explicit is False
    assert got.provider == "azure"
    assert got.azure_project == PROJECT
    assert got.azure_repo == REPO
    assert got.mr_iid == 12
    assert got.project_id == azure_project_num(PROJECT, REPO)
    assert got.source_branch == "feat"
    assert got.target_branch == "main"
    assert got.sha == "abc123"
    assert azure_mr_key(PROJECT, REPO, 12) == f"{got.project_id}-12"


def test_pr_created_stores_collection_from_containers() -> None:
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    got = classify_azure_webhook(
        {
            "eventType": "git.pullrequest.created",
            "resourceContainers": {
                "collection": {"baseUrl": "https://tfs02.company.com.tr/tfs/ExampleCollection/"}
            },
            "resource": pr,
        },
        bot_user_id="bot-guid",
        mention_names=["Creasy"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.azure_collection == "https://tfs02.company.com.tr/tfs/ExampleCollection"


def test_assigning_domain_unique_name_matches_mention_tail():
    pr = _pr()
    pr["reviewers"] = [
        {
            "id": "guid-1",
            "displayName": "Berat Ersari",
            "uniqueName": r"ORGANIZATION\mberatersari",
        }
    ]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": r"Dev added ORGANIZATION\mberatersari as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id=None,
        mention_names=["testuser", "mberatersari"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"


def test_assigning_domain_unique_name_in_html_message():
    pr = _pr()
    pr["reviewers"] = [{"uniqueName": r"ORGANIZATION\mberatersari"}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {
            "html": r'<div>Dev added <a>ORGANIZATION\mberatersari</a> as a reviewer</div>',
        },
        "resource": pr,
    }
    got = classify_azure_webhook(payload, mention_names=["mberatersari"])
    assert isinstance(got, ReviewTrigger)


def test_changed_reviewer_list_self_assign_starts_review():
    pr = _pr()
    pr["reviewers"] = [
        {
            "id": "71440e05-be9e-4768-897e-da81a889d26e",
            "displayName": "Berat ERSARI",
            "uniqueName": r"company\mberatersari",
        }
    ]
    payload = {
        "eventType": "git.pullrequest.updated",
        "message": {
            "text": (
                "Berat ERSARI changed the reviewer list for pull request 26509 "
                "(Added sacmalilkarr) in AKBGPIOCaller"
            )
        },
        "resource": pr,
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id="e0782cea-2b9a-414f-8b2f-84a7dd8de5c2",
        mention_names=["test", "mberatersari", "Berat ERSARI"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"


def test_changed_reviewer_list_by_teammate_starts_on_first_sighting():
    pr = _pr()
    pr["reviewers"] = [
        {"id": "bot", "displayName": "Berat ERSARI", "uniqueName": r"company\mberatersari"},
        {"id": "alice", "displayName": "Alice"},
    ]
    payload = {
        "eventType": "git.pullrequest.updated",
        "message": {"text": "Alice changed the reviewer list for pull request 26509"},
        "resource": pr,
    }
    got = classify_azure_webhook(
        payload,
        mention_names=["mberatersari", "Berat ERSARI"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"


def test_self_assign_yourself_message_starts_review():
    pr = _pr()
    pr["reviewers"] = [{"uniqueName": r"ORGANIZATION\mberatersari"}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": r"ORGANIZATION\mberatersari added yourself as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, mention_names=["mberatersari"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"


def test_assigning_bot_as_reviewer_starts_review():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": "Jamal Hartnett added Creasy as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"
    assert got.explicit is True


def test_adding_another_reviewer_while_bot_listed_is_ignored():
    pr = _pr()
    pr["reviewers"] = [
        {"id": "bot-guid", "displayName": "Creasy"},
        {"id": "alice-guid", "displayName": "Alice"},
    ]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": "Jamal Hartnett added Alice as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, Ignore)


def test_adding_required_teammate_while_bot_listed_is_ignored():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    payload = {
        "eventType": "git.pullrequest.reviewers.update",
        "message": {"text": "Dev added Bob as a required reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, Ignore)


def test_teammate_unassign_after_open_does_not_start_review():
    pr = _pr()
    pr["reviewers"] = [
        {"id": "bot", "displayName": "Creasy"},
        {"id": "alice", "displayName": "Alice"},
    ]
    created = classify_azure_webhook(
        {"eventType": "git.pullrequest.created", "resource": pr},
        mention_names=["Creasy"],
    )
    assert isinstance(created, ReviewTrigger)
    later = _pr()
    later["reviewers"] = [
        {"id": "bot", "displayName": "Creasy"},
    ]
    got = classify_azure_webhook(
        {
            "eventType": "git.pullrequest.updated",
            "message": {"text": "Alice changed the reviewer list for pull request 12"},
            "resource": later,
        },
        mention_names=["Creasy"],
    )
    assert isinstance(got, Ignore)


def test_unstructured_add_without_bot_name_is_ignored():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": "Reviewers were updated: Alice was added as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, Ignore)


def test_unstructured_add_with_bot_name_starts_review():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewersUpdateNotification",
        "message": {"text": "Reviewers were updated: Creasy was added as a reviewer"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "review"


def test_reviewer_vote_update_is_ignored():
    pr = _pr()
    pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy", "vote": 10}]
    payload = {
        "eventType": "git.pullrequest.updated",
        "notificationType": "ReviewerVoteNotification",
        "message": {"text": "Jamal Hartnett voted on the pull request"},
        "resource": pr,
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["Creasy"])
    assert isinstance(got, Ignore)
    assert got.reason == "action=update"


def test_pr_updated_is_ignored():
    got = classify_azure_webhook({"eventType": "git.pullrequest.updated", "resource": _pr()})
    assert isinstance(got, Ignore)
    assert got.reason == "action=update"


def test_pr_abandoned_is_cleanup():
    got = classify_azure_webhook(
        {"eventType": "git.pullrequest.updated", "resource": _pr(status="abandoned")}
    )
    assert isinstance(got, CleanupTrigger)
    assert got.action == "close"
    assert got.mr_iid == 12


def test_pr_merged_is_cleanup():
    got = classify_azure_webhook({"eventType": "git.pullrequest.merged", "resource": _pr(status="completed")})
    assert isinstance(got, CleanupTrigger)
    assert got.action == "merge"


def test_mention_without_command_is_usage():
    payload = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {"content": "@creasy please check auth", "author": {"id": "user-1"}},
            "pullRequest": _pr(),
        },
    }
    got = classify_azure_webhook(payload, bot_user_id="bot-guid", mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "usage"
    assert got.explicit is True
    html = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {
                "content": (
                    '<a href="#" data-vss-mention="version:2.0,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">'
                    "@Creasy</a> look at this"
                ),
                "author": {"id": "user-1"},
            },
            "pullRequest": _pr(),
        },
    }
    tagged = classify_azure_webhook(
        html,
        bot_user_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        mention_names=[],
    )
    assert isinstance(tagged, ReviewTrigger)
    assert tagged.kind == "usage"
    other = classify_azure_webhook(payload, mention_names=["other-bot"])
    assert isinstance(other, Ignore)


def test_file_comment_keeps_range_and_question():
    payload = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {
                "id": 3,
                "content": "@creasy /ask is this lock safe?",
                "author": {"id": "user-1"},
                "threadContext": {
                    "filePath": "/src/lock.cpp",
                    "rightFileStart": {"line": 40, "offset": 1},
                    "rightFileEnd": {"line": 52, "offset": 1},
                },
                "_links": {
                    "self": {
                        "href": (
                            "https://ado.example/_apis/git/repositories/"
                            f"{REPO}/pullRequests/12/threads/9/comments/3"
                        )
                    }
                },
            },
            "pullRequest": _pr(),
        },
    }
    got = classify_azure_webhook(payload, mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert got.discussion_id == "9"
    assert got.parent_comment_id == 3
    nested_parent = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {
                "id": 8,
                "parentCommentId": 1,
                "content": "@creasy /ask why dest?",
                "author": {"id": "user-1"},
                "_links": {
                    "self": {
                        "href": (
                            "https://ado.example/_apis/git/repositories/"
                            f"{REPO}/pullRequests/12/threads/9/comments/8"
                        )
                    }
                },
            },
            "pullRequest": _pr(),
        },
    }
    reply = classify_azure_webhook(nested_parent, mention_names=["creasy"])
    assert isinstance(reply, ReviewTrigger)
    assert reply.discussion_id == "9"
    assert reply.parent_comment_id == 8
    assert got.comment_path == "src/lock.cpp"
    assert got.comment_start_line == 40
    assert got.comment_end_line == 52
    assert "lock" in got.comment_text


def test_comment_event_resource_is_the_comment():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "id": 8,
            "content": "@mberatersari /ask asdfasf",
            "author": {"id": "71440e05-be9e-4768-897e-da81a889d26e"},
            "pullRequest": _pr(
                reviewers=[
                    {
                        "id": "71440e05-be9e-4768-897e-da81a889d26e",
                        "uniqueName": r"company\mberatersari",
                    }
                ]
            ),
        },
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id="e0782cea-2b9a-414f-8b2f-84a7dd8de5c2",
        mention_names=["mberatersari", "Berat ERSARI"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert "asdfasf" in got.comment_text


def test_comment_vss_mention_uses_reviewer_id_not_connectiondata_id():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "content": (
                    '<a href="#" data-vss-mention="version:2.0,71440e05-be9e-4768-897e-da81a889d26e">'
                    "@Berat ERSARI</a> /ask why this lock?"
                ),
                "author": {"id": "someone-else"},
            },
            "pullRequest": _pr(
                reviewers=[
                    {
                        "id": "71440e05-be9e-4768-897e-da81a889d26e",
                        "displayName": "Berat ERSARI",
                        "uniqueName": r"company\mberatersari",
                    }
                ]
            ),
        },
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id="e0782cea-2b9a-414f-8b2f-84a7dd8de5c2",
        mention_names=["mberatersari"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"


def test_comment_tfs_angle_guid_mention_starts_ask():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "id": 8,
            "content": (
                "@<71440E05-BE9E-4768-897E-DA81A889D26E> /ask hey buradaki sorun ne"
            ),
            "author": {"id": "71440e05-be9e-4768-897e-da81a889d26e"},
            "pullRequest": _pr(
                reviewers=[
                    {
                        "id": "71440e05-be9e-4768-897e-da81a889d26e",
                        "displayName": "Berat ERSARI",
                        "uniqueName": r"company\mberatersari",
                    }
                ]
            ),
        },
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id="e0782cea-2b9a-414f-8b2f-84a7dd8de5c2",
        mention_names=["sa_mirai_project", "mberatersari", "Berat"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert got.comment_text == "hey buradaki sorun ne"


def test_comment_html_ask_without_space_after_mention():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "content": (
                    '<a href="#" data-vss-mention="version:2.0,71440e05-be9e-4768-897e-da81a889d26e">'
                    '@Berat ERSARI</a>/ask "why this lock?"'
                ),
                "author": {"id": "someone-else"},
            },
            "pullRequest": _pr(
                reviewers=[
                    {
                        "id": "71440e05-be9e-4768-897e-da81a889d26e",
                        "displayName": "Berat ERSARI",
                        "uniqueName": r"company\mberatersari",
                    }
                ]
            ),
        },
    }
    got = classify_azure_webhook(
        payload,
        bot_user_id="e0782cea-2b9a-414f-8b2f-84a7dd8de5c2",
        mention_names=["mberatersari"],
    )
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert "why this lock" in got.comment_text


def test_comment_plain_ask_quoted_question():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "content": '@mberatersari /ask "question"',
            "author": {"id": "user-1"},
            "pullRequest": _pr(),
        },
    }
    got = classify_azure_webhook(payload, mention_names=["mberatersari"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert "question" in got.comment_text


def test_comment_review_and_ask():
    payload = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {"content": "@creasy /ask focus on auth", "author": {"id": "user-1"}},
            "pullRequest": _pr(),
        },
    }
    got = classify_azure_webhook(payload, bot_user_id="bot", mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert got.explicit is True
    assert got.comment_text == "focus on auth"
    ask = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {"content": "@creasy /ask? why this lock?", "author": {"id": "user-1"}},
            "pullRequest": _pr(),
        },
    }
    got_ask = classify_azure_webhook(ask, mention_names=["creasy"])
    assert isinstance(got_ask, ReviewTrigger)
    assert got_ask.kind == "ask"
    assert "lock" in got_ask.comment_text


def test_bot_comment_and_edit_and_empty_ask_ignored():
    bot = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {"content": "@creasy /ask why", "author": {"id": "bot-id"}},
            "pullRequest": _pr(),
        },
    }
    assert isinstance(classify_azure_webhook(bot, bot_user_id="bot-id", mention_names=["creasy"]), Ignore)
    got_bot = classify_azure_webhook(bot, bot_user_id=None, mention_names=["creasy"])
    assert isinstance(got_bot, ReviewTrigger)
    assert got_bot.kind == "ask"
    created_with_updated = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "message": {"text": "Jamal Hartnett commented"},
        "resource": {
            "comment": {
                "content": "@creasy /ask why",
                "author": {"id": "user-1"},
                "publishedDate": "2026-01-01T00:00:00.000Z",
                "lastUpdatedDate": "2026-01-01T00:00:00.400Z",
            },
            "pullRequest": _pr(),
        },
    }
    created_got = classify_azure_webhook(created_with_updated, mention_names=["creasy"])
    assert isinstance(created_got, ReviewTrigger)
    assert created_got.kind == "ask"
    edited = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "message": {"text": "Jamal Hartnett has edited a pull request comment"},
        "resource": {
            "comment": {
                "content": "/review",
                "author": {"id": "user-1"},
                "publishedDate": "2026-01-01T00:00:00Z",
                "lastUpdatedDate": "2026-01-01T01:00:00Z",
                "lastContentUpdatedDate": "2026-01-01T01:00:00Z",
            },
            "pullRequest": _pr(),
        },
    }
    assert isinstance(classify_azure_webhook(edited), Ignore)
    empty = {
        "eventType": "git.pullrequest.commented",
        "resource": {"comment": {"content": "@creasy /ask   ", "author": {"id": "u"}}, "pullRequest": _pr()},
    }
    assert isinstance(classify_azure_webhook(empty, mention_names=["creasy"]), Ignore)


def test_azure_question_before_ask_is_not_empty():
    payload = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {
                "content": "This overflow looks wrong.\n@creasy /ask",
                "author": {"id": "user-1"},
            },
            "pullRequest": _pr(),
        },
    }
    got = classify_azure_webhook(payload, mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert "overflow" in got.comment_text


def test_comment_without_nested_pr_uses_links_and_containers():
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "message": {"text": "Jamal commented"},
        "resourceContainers": {"project": {"id": PROJECT, "name": "App"}},
        "resource": {
            "comment": {
                "content": "@creasy /ask focus on auth",
                "author": {"id": "user-1"},
                "publishedDate": "2026-01-01T00:00:00Z",
                "lastUpdatedDate": "2026-01-01T00:00:00Z",
                "_links": {
                    "self": {
                        "href": (
                            "https://ado.example/tfs/DefaultCollection/_apis/git"
                            f"/repositories/{REPO}/pullRequests/12/threads/5/comments/1"
                        )
                    }
                },
            }
        },
    }
    got = classify_azure_webhook(payload, mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.kind == "ask"
    assert got.mr_iid == 12
    assert got.azure_repo == REPO
    assert got.azure_project == PROJECT
    assert got.comment_text == "focus on auth"
    assert got.discussion_id == "5"
    assert got.parent_comment_id == 1


def test_draft_created_skipped_explicit_allowed():
    draft_pr = _pr(isDraft=True)
    draft_pr["reviewers"] = [{"id": "bot-guid", "displayName": "Creasy"}]
    draft = classify_azure_webhook(
        {"eventType": "git.pullrequest.created", "resource": draft_pr},
        skip_drafts=True,
        bot_user_id="bot-guid",
        mention_names=["Creasy"],
    )
    assert isinstance(draft, Ignore)
    note = {
        "eventType": "git.pullrequest.commented",
        "resource": {
            "comment": {"content": "@creasy /ask please look", "author": {"id": "u"}},
            "pullRequest": _pr(isDraft=True),
        },
    }
    got = classify_azure_webhook(note, skip_drafts=True, mention_names=["creasy"])
    assert isinstance(got, ReviewTrigger)
    assert got.explicit is True


def test_unknown_event_ignored():
    assert isinstance(classify_azure_webhook({"eventType": "workitem.created"}), Ignore)
    assert isinstance(classify_azure_webhook({}), Ignore)