from __future__ import annotations

import json
import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opencode_manager.gitlab.events import (
    CleanupTrigger,
    Ignore,
    ReviewTrigger,
    apply_live_reviewers,
    classify_webhook,
    _project_and_mr,
)
from opencode_manager.review_log import get_logger, log_fail, log_ok, redact_userinfo
from opencode_manager.review.mention import collect_names, parse_mention_aliases

router = APIRouter()
logger = get_logger("webhook")


def _bot_user_id(request: Request) -> Optional[int]:
    cached = getattr(request.app.state, "bot_user_id", None)
    if cached is not None:
        return cached
    gitlab = getattr(request.app.state, "gitlab", None)
    if gitlab is None:
        return None
    uid = gitlab.current_user_id()
    if uid is not None:
        request.app.state.bot_user_id = uid
    return uid


def _mention_names(request: Request) -> list[str]:
    cfg = request.app.state.config
    cached = getattr(request.app.state, "bot_mention_names", None) or []
    gitlab = getattr(request.app.state, "gitlab", None)
    live: list[str] = []
    current = getattr(gitlab, "current_user", None) if gitlab is not None else None
    if callable(current):
        user = current()
        if isinstance(user, dict):
            live = list(user.get("names") or [])
            if live:
                request.app.state.bot_mention_names = live
    return collect_names(parse_mention_aliases(getattr(cfg, "review_mention", "")), cached, live)


_DROP_KEYS = frozenset(
    {
        "description",
        "last_commit",
        "work_in_progress",
        "st_commits",
        "st_diffs",
        "total_time_spent",
        "time_change",
        "human_total_time_spent",
        "human_time_change",
    }
)
_BODY_LIMIT = 8000


def _slim(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _DROP_KEYS:
                continue
            out[str(key)] = _slim(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_slim(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        text = redact_userinfo(value)
        if len(text) > 400:
            return text[:400] + "..."
        return text
    return value


def _log_incoming_gitlab(payload: dict[str, Any], *, bot_id: Optional[int], mention_names: list[str]) -> None:
    attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    reviewers = payload.get("reviewers") if isinstance(payload.get("reviewers"), list) else []
    names = []
    for row in reviewers:
        if isinstance(row, dict):
            names.append(str(row.get("username") or row.get("id") or "-"))
    log_ok(
        logger,
        "webhook gitlab incoming",
        object_kind=str(payload.get("object_kind") or "-"),
        action=str((attrs or {}).get("action") or "-"),
        project=(attrs or {}).get("target_project_id") or payload.get("project_id") or "-",
        mr=(attrs or {}).get("iid") or "-",
        change_keys=",".join(sorted(str(k) for k in changes.keys())) or "-",
        reviewers=",".join(names) or "-",
        bot_id=bot_id if bot_id is not None else "-",
        mention_names=",".join(mention_names) or "-",
    )
    blob = redact_userinfo(json.dumps(_slim(payload), ensure_ascii=False, default=str))
    if len(blob) > _BODY_LIMIT:
        blob = blob[:_BODY_LIMIT] + f"...(+{len(blob) - _BODY_LIMIT} chars)"
    logger.info("webhook gitlab body %s", blob)


def _verify_secret(request: Request) -> None:
    secret = request.app.state.config.gitlab_webhook_secret
    if not secret:
        log_ok(logger, "webhook secret", check="skipped", reason="WEBHOOK_SECRET unset")
        return
    got = request.headers.get("X-Gitlab-Token", "")
    if got != secret:
        log_fail(logger, "webhook secret", reason="mismatch")
        raise HTTPException(status_code=401, detail="Invalid secret")
    log_ok(logger, "webhook secret", check="matched")


@router.post("/amirmini/webhook/gitlab")
async def webhook(request: Request) -> JSONResponse:
    _verify_secret(request)
    try:
        payload = await request.json()
    except Exception as exc:
        log_fail(logger, "webhook JSON", err=exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        log_fail(logger, "webhook JSON", reason="not an object")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    config = request.app.state.config
    manager = request.app.state.review_manager
    bot_id = _bot_user_id(request)
    mention_names = _mention_names(request)
    kind = str(payload.get("object_kind") or "").strip().lower()
    _log_incoming_gitlab(payload, bot_id=bot_id, mention_names=mention_names)
    if kind == "merge_request":
        attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
        action = str((attrs or {}).get("action") or "").strip().lower()
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        have_change = "reviewers" in changes or "reviewer_ids" in changes
        have_rows = isinstance(payload.get("reviewers"), list) and bool(payload.get("reviewers"))
        gitlab = getattr(request.app.state, "gitlab", None)
        if action == "update" and not have_change and not have_rows and gitlab is not None:
            ids = _project_and_mr(payload)
            fetch = getattr(gitlab, "list_reviewers", None)
            if ids and callable(fetch):
                live = fetch(ids[0], ids[1])
                if live:
                    apply_live_reviewers(payload, live)
    if kind == "note" and bot_id is None and not mention_names:
        log_fail(logger, "webhook bot user", reason="GITLAB_TOKEN user unknown")
        return JSONResponse({"status": "ignored", "reason": "bot user unknown"})
    classified = classify_webhook(
        payload,
        skip_drafts=config.skip_draft_mrs,
        bot_user_id=bot_id,
        mention_names=mention_names,
    )

    if isinstance(classified, Ignore):
        log_ok(logger, "webhook ignored", object_kind=kind or "missing", reason=classified.reason)
        return JSONResponse({"status": "ignored", "reason": classified.reason})

    if isinstance(classified, CleanupTrigger):
        threading.Thread(
            target=manager.cleanup_mr,
            args=(classified,),
            name=f"cleanup-{classified.project_id}-{classified.mr_iid}",
            daemon=True,
        ).start()
        log_ok(
            logger,
            "webhook cleanup started",
            action=classified.action,
            project=classified.project_id,
            mr=classified.mr_iid,
        )
        return JSONResponse(
            {
                "status": "accepted",
                "action": "cleanup",
                "project_id": classified.project_id,
                "mr_iid": classified.mr_iid,
            }
        )

    if isinstance(classified, ReviewTrigger):
        ack, job, message = manager.submit(classified)
        body = {"status": ack, "message": message}
        if job:
            body["job_id"] = job.job_id
            body["mr_key"] = job.mr_key
        fields = {
            "ack": ack,
            "kind": classified.kind,
            "project": classified.project_id,
            "mr": classified.mr_iid,
            "job": job.job_id if job else "-",
            "message": message,
        }
        if ack == "ignored":
            log_fail(logger, "webhook submit", **fields)
        else:
            log_ok(logger, "webhook submit", **fields)
        return JSONResponse(body)

    log_ok(logger, "webhook ignored", object_kind=kind or "missing", reason="unhandled")
    return JSONResponse({"status": "ignored", "reason": "unhandled"})
