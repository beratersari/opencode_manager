"""Terminal POST to the request callback_url."""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx

from opencode_manager.log import clip, get_logger, log_fail, log_http, redact
from opencode_manager.models import Envelope
from opencode_manager.settings import Settings

logger = get_logger()


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
            if response.status_code < 500:
                return
            last_err = f"HTTP {response.status_code} {clip(response.text, 200)}"
        except Exception as exc:  # noqa: BLE001
            last_err = redact(str(exc))
            log_http(logger, "POST", callback_url, err=last_err, ok=False)
            logger.warning("callback failed: %s", last_err)
        time.sleep(min(2 ** index, 8))
    log_fail(logger, "callback gave up", attempts=attempts, err=last_err, url=callback_url)
