"""Azure DevOps Service Hook receiver. Isolated from POST /webhook."""

from __future__ import annotations

import asyncio
import base64
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opencode_manager.azure.client import AzureError
from opencode_manager.azure.events import (
    apply_live_reviewers,
    azure_collection_hint,
    azure_is_reviewer_list_event,
    azure_message_adds_bot,
    azure_pr_locator,
    azure_pr_web_url,
    azure_reviewers_include_bot,
    classify_azure_webhook,
)
from opencode_manager.gitlab.events import CleanupTrigger, Ignore, ReviewTrigger
from opencode_manager.review_log import get_logger, log_fail, log_ok
from opencode_manager.review.mention import collect_names, parse_mention_aliases

router = APIRouter()
logger = get_logger("webhook.azure")

# After an "added" hook, TFS may not have committed the reviewer yet.
REVIEWER_GET_RETRY_DELAYS = (0.3, 0.7)


def _azure_bot_id(request: Request) -> Optional[str]:
    cached = getattr(request.app.state, "azure_bot_user_id", None)
    if cached is not None:
        return str(cached)
    azure = getattr(request.app.state, "azure", None)
    if azure is None:
        return None
    uid = azure.current_user_id()
    if uid:
        request.app.state.azure_bot_user_id = uid
    return uid


def _mention_names(request: Request) -> list[str]:
    cfg = request.app.state.config
    cached = getattr(request.app.state, "azure_bot_mention_names", None) or []
    azure = getattr(request.app.state, "azure", None)
    live: list[str] = []
    current = getattr(azure, "current_user", None) if azure is not None else None
    if callable(current):
        user = current()
        if isinstance(user, dict):
            live = list(user.get("names") or [])
            if live:
                request.app.state.azure_bot_mention_names = live
    return collect_names(parse_mention_aliases(getattr(cfg, "review_mention", "")), cached, live)


def _verify_secret(request: Request) -> None:
    config = request.app.state.config
    password = (config.azure_webhook_password or "").strip()
    if not password:
        log_ok(logger, "azure webhook secret", check="skipped", reason="AZURE_WEBHOOK_PASSWORD unset")
        return
    header = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    expected_user = (config.azure_webhook_user or "").encode("utf-8")
    expected_pass = password.encode("utf-8")
    if not header.lower().startswith("basic "):
        log_fail(logger, "azure webhook secret", reason="missing Basic auth")
        raise HTTPException(status_code=401, detail="Invalid secret")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except Exception as exc:
        log_fail(logger, "azure webhook secret", reason="invalid Basic auth", err=exc)
        raise HTTPException(status_code=401, detail="Invalid secret") from exc
    user, _, got = decoded.partition(":")
    if got.encode("utf-8") != expected_pass:
        log_fail(logger, "azure webhook secret", reason="password mismatch")
        raise HTTPException(status_code=401, detail="Invalid secret")
    if expected_user and user.encode("utf-8") != expected_user:
        log_fail(logger, "azure webhook secret", reason="user mismatch")
        raise HTTPException(status_code=401, detail="Invalid secret")
    log_ok(logger, "azure webhook secret", check="matched")


async def _fetch_reviewers(
    azure: object,
    locator: tuple[str, str, int],
    payload: dict,
    *,
    bot_id: Optional[str],
    mention_names: list[str],
    collection: str,
    web_url: str,
) -> list:
    delays = (0.0,) + tuple(REVIEWER_GET_RETRY_DELAYS)
    last: list = []
    last_error: Optional[AzureError] = None
    for index, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            last = azure.list_reviewers(  # type: ignore[attr-defined]
                *locator,
                collection=collection,
                web_url=web_url,
            )
        except TypeError:
            last = azure.list_reviewers(*locator)  # type: ignore[attr-defined]
        except AzureError as exc:
            last_error = exc
            if index == len(delays) - 1:
                raise
            log_ok(logger, "azure reviewers GET retry", attempt=index + 1, err=exc)
            continue
        last_error = None
        if azure_reviewers_include_bot(last, bot_id, mention_names):
            if index:
                log_ok(logger, "azure reviewers GET listed after retry", attempt=index + 1)
            return last
        if not azure_message_adds_bot(payload, bot_id, mention_names):
            return last
        log_ok(logger, "azure reviewers GET empty on add, retry", attempt=index + 1)
    if last_error is not None:
        raise last_error
    return last


@router.post("/amirmini/webhook/azure")
async def webhook_azure(request: Request) -> JSONResponse:
    _verify_secret(request)
    config = request.app.state.config
    try:
        payload = await request.json()
    except Exception as exc:
        log_fail(logger, "azure webhook JSON", err=exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        log_fail(logger, "azure webhook JSON", reason="not an object")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    manager = request.app.state.review_manager
    bot_id = _azure_bot_id(request)
    mention_names = _mention_names(request)
    logger.info(
        "azure webhook identity bot_id=%s mention_names=%s review_mention=%s eventType=%s",
        bot_id or "-",
        ",".join(mention_names) or "-",
        (config.review_mention or "").strip() or "-",
        payload.get("eventType") or payload.get("event_type") or "-",
    )
    if azure_is_reviewer_list_event(payload):
        azure = getattr(request.app.state, "azure", None)
        locator = azure_pr_locator(payload)
        if azure is None or locator is None or not callable(getattr(azure, "list_reviewers", None)):
            log_fail(logger, "azure reviewers GET", reason="no client or PR locator")
            return JSONResponse({"status": "ignored", "reason": "reviewers GET unavailable"})
        collection = azure_collection_hint(payload)
        web_url = azure_pr_web_url(payload)
        apply = getattr(azure, "apply_collection", None)
        if callable(apply):
            apply(collection, web_url)
        try:
            live = await _fetch_reviewers(
                azure,
                locator,
                payload,
                bot_id=bot_id,
                mention_names=mention_names,
                collection=collection,
                web_url=web_url,
            )
        except AzureError as exc:
            log_fail(logger, "azure reviewers GET", err=exc, pr=locator[2])
            live = []
        apply_live_reviewers(payload, live)
        log_ok(
            logger,
            "azure reviewers GET",
            project=locator[0],
            repo=locator[1],
            pr=locator[2],
            count=len(live),
        )
    classified = classify_azure_webhook(
        payload,
        skip_drafts=config.skip_draft_mrs,
        bot_user_id=bot_id,
        mention_names=mention_names,
    )
    logger.info(
        "azure webhook classified=%s eventType=%s",
        type(classified).__name__,
        payload.get("eventType") or payload.get("event_type") or "-",
    )

    if isinstance(classified, Ignore):
        log_ok(logger, "azure webhook ignored", reason=classified.reason)
        return JSONResponse({"status": "ignored", "reason": classified.reason})

    if isinstance(classified, CleanupTrigger):
        threading.Thread(
            target=manager.cleanup_mr,
            args=(classified,),
            name=f"cleanup-azure-{classified.project_id}-{classified.mr_iid}",
            daemon=True,
        ).start()
        log_ok(
            logger,
            "azure webhook cleanup started",
            action=classified.action,
            project_num=classified.project_id,
            pr=classified.mr_iid,
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
        azure = getattr(request.app.state, "azure", None)
        apply = getattr(azure, "apply_collection", None) if azure is not None else None
        if callable(apply):
            apply(classified.azure_collection, classified.web_url)
        ack, job, message = manager.submit(classified)
        body = {"status": ack, "message": message}
        if job:
            body["job_id"] = job.job_id
            body["mr_key"] = job.mr_key
        fields = {
            "ack": ack,
            "kind": classified.kind,
            "pr": classified.mr_iid,
            "azure_project": classified.azure_project,
            "azure_repo": classified.azure_repo,
            "job": job.job_id if job else "-",
            "message": message,
        }
        if ack == "ignored":
            log_fail(logger, "azure webhook submit", **fields)
        else:
            log_ok(logger, "azure webhook submit", **fields)
        return JSONResponse(body)

    log_ok(logger, "azure webhook ignored", reason="unhandled")
    return JSONResponse({"status": "ignored", "reason": "unhandled"})
