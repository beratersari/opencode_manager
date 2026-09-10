from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from opencode_manager.review_log import get_logger, log_ok
from opencode_manager.review.comment_range import parse_gitlab_position
from opencode_manager.review.mention import comment_intent, first_slash_command, is_usage_note, user_comment_text

logger = get_logger("gitlab.events")

TriggerKind = Literal["open", "update", "reopen", "review", "ask", "reset", "usage"]


@dataclass(frozen=True)
class ReviewTrigger:
    kind: TriggerKind
    project_id: int
    mr_iid: int
    source_branch: str = ""
    target_branch: str = ""
    sha: str = ""
    comment_text: str = ""
    web_url: str = ""
    title: str = ""
    draft: bool = False
    explicit: bool = False
    provider: str = "gitlab"
    azure_project: str = ""
    azure_repo: str = ""
    azure_collection: str = ""
    discussion_id: str = ""
    parent_comment_id: int = 0
    comment_path: str = ""
    comment_side: str = ""
    comment_start_line: int = 0
    comment_end_line: int = 0
    parent_comment_text: str = ""
    source: str = ""


@dataclass(frozen=True)
class CleanupTrigger:
    project_id: int
    mr_iid: int
    action: str


@dataclass(frozen=True)
class Ignore:
    reason: str


Classified = Union[ReviewTrigger, CleanupTrigger, Ignore]


def first_command(body: str) -> Optional[tuple[str, str]]:
    """Return (command, remainder) for the first /ask token."""
    return first_slash_command(body)


def _attrs(payload: dict[str, Any]) -> dict[str, Any]:
    attrs = payload.get("object_attributes") or {}
    return attrs if isinstance(attrs, dict) else {}


def _project_source(payload: dict[str, Any]) -> str:
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    for key in ("path_with_namespace", "pathWithNamespace", "name_with_namespace"):
        text = str(project.get(key) or "").strip()
        if text:
            return text
    return str(project.get("path") or project.get("name") or "").strip()


def _project_and_mr(payload: dict[str, Any]) -> Optional[tuple[int, int]]:
    attrs = _attrs(payload)
    mr = payload.get("merge_request")
    mr = mr if isinstance(mr, dict) else {}
    project_id = (
        attrs.get("target_project_id")
        or attrs.get("source_project_id")
        or mr.get("target_project_id")
        or (payload.get("project") or {}).get("id")
    )
    iid = attrs.get("iid") or mr.get("iid")
    try:
        if project_id is None or iid is None:
            return None
        return int(project_id), int(iid)
    except (TypeError, ValueError):
        return None


def _is_draft(payload: dict[str, Any], attrs: dict[str, Any]) -> bool:
    mr = payload.get("merge_request")
    mr = mr if isinstance(mr, dict) else {}
    for blob in (attrs, mr):
        if blob.get("draft") is True or blob.get("work_in_progress") is True:
            return True
    return False


def classify_webhook(
    payload: dict[str, Any],
    *,
    skip_drafts: bool = True,
    bot_user_id: Optional[int] = None,
    mention_names: Optional[list[str]] = None,
) -> Classified:
    if not isinstance(payload, dict):
        result: Classified = Ignore("invalid payload")
    else:
        kind = str(payload.get("object_kind") or "").strip().lower()
        if kind == "merge_request":
            result = _classify_merge_request(
                payload,
                skip_drafts=skip_drafts,
                bot_user_id=bot_user_id,
                mention_names=mention_names or [],
            )
        elif kind == "note":
            result = _classify_note(
                payload,
                bot_user_id=bot_user_id,
                mention_names=mention_names or [],
            )
        else:
            result = Ignore(f"object_kind={kind or 'missing'}")
    _log_classified(result, payload if isinstance(payload, dict) else {})
    return result


def _log_classified(result: Classified, payload: dict[str, Any]) -> None:
    kind = str(payload.get("object_kind") or "").strip().lower() or "missing"
    if isinstance(result, Ignore):
        log_ok(logger, "gitlab classify Ignore", object_kind=kind, reason=result.reason)
        return
    if isinstance(result, CleanupTrigger):
        log_ok(
            logger,
            "gitlab classify Cleanup",
            object_kind=kind,
            action=result.action,
            project=result.project_id,
            mr=result.mr_iid,
        )
        return
    log_ok(
        logger,
        "gitlab classify ReviewTrigger",
        object_kind=kind,
        kind=result.kind,
        project=result.project_id,
        mr=result.mr_iid,
        explicit=result.explicit,
        draft=result.draft,
        title=result.title or "-",
        sha=result.sha or "-",
    )


def _classify_merge_request(
    payload: dict[str, Any],
    *,
    skip_drafts: bool,
    bot_user_id: Optional[int],
    mention_names: Optional[list[str]] = None,
) -> Classified:
    attrs = _attrs(payload)
    action = str(attrs.get("action") or "").strip().lower()
    ids = _project_and_mr(payload)
    if ids is None:
        return Ignore("missing project_id or mr_iid")
    project_id, mr_iid = ids
    if action in {"close", "merge"}:
        return CleanupTrigger(project_id=project_id, mr_iid=mr_iid, action=action)
    if action == "update":
        return _classify_reviewer_assigned(
            payload,
            attrs,
            skip_drafts=skip_drafts,
            bot_user_id=bot_user_id,
            mention_names=mention_names or [],
        )
    if action != "open":
        return Ignore(f"action={action or 'missing'}")
    if not _bot_listed_as_reviewer(payload, attrs, bot_user_id, mention_names or []):
        return Ignore("reviewer not assigned")
    draft = _is_draft(payload, attrs)
    if skip_drafts and draft:
        return Ignore("draft MR")
    return ReviewTrigger(
        kind=action,  # type: ignore[arg-type]
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=str(attrs.get("source_branch") or ""),
        target_branch=str(attrs.get("target_branch") or ""),
        sha=str((attrs.get("last_commit") or {}).get("id") or "")
        if isinstance(attrs.get("last_commit"), dict)
        else "",
        web_url=str(attrs.get("url") or ""),
        title=str(attrs.get("title") or ""),
        draft=draft,
        explicit=False,
        source=_project_source(payload),
    )


def _name_aliases(names: list[str]) -> set[str]:
    return {str(name).strip().lower() for name in names if str(name).strip()}


def _gitlab_reviewer_rows(payload: dict[str, Any], attrs: dict[str, Any]) -> list[Any]:
    rows: list[Any] = []
    for raw in (payload.get("reviewers"), attrs.get("reviewers"), attrs.get("reviewer_ids")):
        if isinstance(raw, list):
            rows.extend(raw)
    return rows


def _bot_listed_as_reviewer(
    payload: dict[str, Any],
    attrs: dict[str, Any],
    bot_user_id: Optional[int],
    mention_names: list[str],
) -> bool:
    return bool(_matching_reviewer_ids(_gitlab_reviewer_rows(payload, attrs), bot_user_id, mention_names))


def _reviewer_matches(row: Any, bot_user_id: Optional[int], names: list[str]) -> bool:
    if isinstance(row, int) or (isinstance(row, str) and str(row).strip().isdigit()):
        try:
            return bot_user_id is not None and int(row) == bot_user_id
        except (TypeError, ValueError):
            return False
    if not isinstance(row, dict):
        return False
    try:
        if bot_user_id is not None and row.get("id") is not None and int(row["id"]) == bot_user_id:
            return True
    except (TypeError, ValueError):
        pass
    aliases = _name_aliases(names)
    for key in ("username", "name"):
        value = str(row.get(key) or "").strip().lower()
        if value and value in aliases:
            return True
    return False


def _matching_reviewer_ids(raw: Any, bot_user_id: Optional[int], names: list[str]) -> set[int]:
    ids: set[int] = set()
    if not isinstance(raw, list):
        return ids
    for index, item in enumerate(raw):
        if not _reviewer_matches(item, bot_user_id, names):
            continue
        value: Any = item.get("id") if isinstance(item, dict) else item
        try:
            ids.add(int(value) if value is not None and str(value).strip() != "" else index)
        except (TypeError, ValueError):
            ids.add(index)
    return ids


def _bot_rerequested(
    current_rows: list[dict[str, Any]],
    bot_user_id: Optional[int],
    names: list[str],
) -> bool:
    for row in current_rows:
        if not _reviewer_matches(row, bot_user_id, names):
            continue
        if row.get("re_requested") is True:
            return True
    return False


def _classify_reviewer_assigned(
    payload: dict[str, Any],
    attrs: dict[str, Any],
    *,
    skip_drafts: bool,
    bot_user_id: Optional[int],
    mention_names: list[str],
) -> Classified:
    if bot_user_id is None and not mention_names:
        return Ignore("action=update")
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    blob = changes.get("reviewers") if "reviewers" in changes else changes.get("reviewer_ids")
    previous_raw: Any = None
    current_raw: Any = None
    current_rows: list[dict[str, Any]] = []
    if isinstance(blob, dict):
        previous_raw, current_raw = blob.get("previous"), blob.get("current")
        current_rows = [r for r in (current_raw or []) if isinstance(r, dict)]
    elif isinstance(blob, list) and len(blob) >= 2:
        previous_raw, current_raw = blob[0], blob[1]
        current_rows = [r for r in (current_raw or []) if isinstance(r, dict)]
    previous = _matching_reviewer_ids(previous_raw, bot_user_id, mention_names)
    current = _matching_reviewer_ids(current_raw, bot_user_id, mention_names)
    added = bool(current - previous)
    rerequested = _bot_rerequested(current_rows, bot_user_id, mention_names)
    if not added and not rerequested:
        return Ignore("action=update")
    ids = _project_and_mr(payload)
    if ids is None:
        return Ignore("missing project_id or mr_iid")
    project_id, mr_iid = ids
    draft = _is_draft(payload, attrs)
    log_ok(logger, "gitlab classify reviewer assigned", project=project_id, mr=mr_iid, bot=bot_user_id)
    return ReviewTrigger(
        kind="review",
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=str(attrs.get("source_branch") or ""),
        target_branch=str(attrs.get("target_branch") or ""),
        sha=str((attrs.get("last_commit") or {}).get("id") or "")
        if isinstance(attrs.get("last_commit"), dict)
        else "",
        web_url=str(attrs.get("url") or ""),
        title=str(attrs.get("title") or ""),
        draft=draft,
        explicit=True,
        source=_project_source(payload),
    )


def _classify_note(
    payload: dict[str, Any],
    *,
    bot_user_id: Optional[int],
    mention_names: list[str],
) -> Classified:
    attrs = _attrs(payload)
    if str(attrs.get("noteable_type") or "") != "MergeRequest":
        return Ignore("note not on merge request")
    # GitLab 16.11+ also fires the Note Hook when a comment is edited.
    # A second /ask from that edit is not a new request.
    action = str(attrs.get("action") or "").strip().lower()
    if action == "update":
        return Ignore("note edit")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    try:
        user_id = int(user.get("id")) if user.get("id") is not None else None
    except (TypeError, ValueError):
        user_id = None
    if bot_user_id is not None and user_id == bot_user_id:
        return Ignore("bot note")
    note_text = str(attrs.get("note") or "")
    if is_usage_note(note_text):
        return Ignore("usage note")
    intent = comment_intent(note_text, mention_names)
    if intent is None:
        return Ignore("no mention+command")
    action, command, remainder = intent
    user_text = user_comment_text(note_text, mention_names)
    if command == "ask" and not remainder and not user_text:
        return Ignore("empty /ask")
    ids = _project_and_mr(payload)
    if ids is None:
        return Ignore("missing project_id or mr_iid")
    project_id, mr_iid = ids
    mr = payload.get("merge_request") if isinstance(payload.get("merge_request"), dict) else {}
    kind: TriggerKind = command  # type: ignore[assignment]
    path, side, start, end = parse_gitlab_position(attrs.get("position") or attrs.get("original_position"))
    return ReviewTrigger(
        kind=kind,
        project_id=project_id,
        mr_iid=mr_iid,
        source_branch=str(mr.get("source_branch") or ""),
        target_branch=str(mr.get("target_branch") or ""),
        sha=str(mr.get("last_commit", {}).get("id") or "")
        if isinstance(mr.get("last_commit"), dict)
        else "",
        comment_text=user_text or remainder,
        web_url=str(mr.get("url") or ""),
        title=str(mr.get("title") or ""),
        draft=_is_draft(payload, attrs),
        explicit=True,
        discussion_id=_gitlab_discussion_id(payload, attrs),
        parent_comment_id=_gitlab_note_id(attrs),
        comment_path=path,
        comment_side=side,
        comment_start_line=start,
        comment_end_line=end,
        parent_comment_text="",
        source=_project_source(payload),
    )


def _gitlab_note_id(attrs: dict[str, Any]) -> int:
    for raw in (attrs.get("id"), attrs.get("note_id"), attrs.get("noteId")):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _gitlab_discussion_id(payload: dict[str, Any], attrs: dict[str, Any]) -> str:
    for raw in (
        attrs.get("discussion_id"),
        attrs.get("discussionId"),
        payload.get("discussion_id"),
        payload.get("discussionId"),
    ):
        text = str(raw or "").strip()
        if text:
            return text
    return ""
