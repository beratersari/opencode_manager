"""Terminal POST to the request callback_url."""

from __future__ import annotations

import time
from typing import Any, Dict, Literal

import httpx

from opencode_manager.log import clip, get_logger, log_fail, log_http, redact
from opencode_manager.models import Envelope
from opencode_manager.settings import Settings

logger = get_logger()

# n8n Wait 404 = webhook not armed yet. 408/429 are transient. Other 4xx are not.
_RETRYABLE_CLIENT = frozenset({404, 408, 429})


def callback_http_outcome(status_code: int) -> Literal["delivered", "retry", "permanent"]:
    if 200 <= status_code < 300:
        return "delivered"
    if status_code in _RETRYABLE_CLIENT or status_code >= 500:
        return "retry"
    return "permanent"


def post_callback(settings: Settings, envelope: Envelope, callback_url: str) -> None:
    payload: Dict[str, Any] = envelope.model_dump()
    last_err = None
    attempts = max(1, settings.callback_retry_count)
    logger.info(
        "callback start url=%s status_code=%s job_id=%s attempts=%s timeout=%ss text_len=%s",
        callback_url,
        envelope.status_code,
        envelope.job_id,
        attempts,
        settings.callback_timeout_seconds,
        len(envelope.text or ""),
    )
    for index in range(attempts):
        try:
            with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
                response = client.post(callback_url, json=payload)
            log_http(
                logger,
                "POST",
                callback_url,
                status=response.status_code,
                body=response.text if response.status_code >= 400 else None,
            )
            logger.info(
                "callback HTTP %s attempt %s/%s",
                response.status_code,
                index + 1,
                attempts,
            )
            outcome = callback_http_outcome(response.status_code)
            if outcome == "delivered":
                return
            last_err = f"HTTP {response.status_code} {clip(response.text, 200)}"
            if outcome == "permanent":
                log_fail(
                    logger,
                    "callback HTTP permanent, not retrying",
                    status=response.status_code,
                    err=last_err,
                    url=callback_url,
                )
                return
        except Exception as exc:  # noqa: BLE001
            last_err = redact(str(exc))
            log_http(logger, "POST", callback_url, err=last_err, ok=False)
            logger.warning("callback failed: %s", last_err)
        if index + 1 < attempts:
            time.sleep(min(2 ** index, 8))
    log_fail(logger, "callback gave up", attempts=attempts, err=last_err, url=callback_url)
