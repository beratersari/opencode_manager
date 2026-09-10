"""Azure DevOps Service Hook receiver. Isolated from POST /webhook."""

from __future__ import annotations

import base64
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opencode_manager.azure.events import classify_azure_webhook
from opencode_manager.azure.urls import has_collection_root, resolve_collection_url
from opencode_manager.gitlab.events import CleanupTrigger, Ignore, ReviewTrigger
from opencode_manager.review_log import get_logger, log_fail, log_ok
from opencode_manager.review.mention import collect_names, parse_mention_aliases

router = APIRouter()
logger = get_logger("webhook.azure")


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


@router.post("/amirmini/webhook/azure")
async def webhook_azure(request: Request) -> JSONResponse:
    _verify_secret(request)
    config = request.app.state.config
    if not config.azure_enabled:
        log_fail(logger, "azure webhook", reason="azure not configured")
        return JSONResponse({"status": "ignored", "reason": "azure not configured"})
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
        resolved = resolve_collection_url(
            configured=config.azure_url,
            collection=classified.azure_collection,
            web_url=classified.web_url,
        )
        if not has_collection_root(resolved):
            log_fail(
                logger,
                "azure webhook collection missing",
                pr=classified.mr_iid,
                configured=config.azure_url or "-",
                hook=classified.azure_collection or "-",
                web=classified.web_url or "-",
            )
            return JSONResponse(
                {
                    "status": "error",
                    "reason": "azure collection missing",
                    "detail": (
                        "Need /tfs/<Collection> on azure_url or on the webhook "
                        "(resourceContainers.collection.baseUrl or the PR _git URL)."
                    ),
                },
                status_code=400,
            )
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
