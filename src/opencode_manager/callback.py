"""Terminal POST to the request callback_url."""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx

from opencode_manager.log import get_logger, redact
from opencode_manager.models import Envelope
from opencode_manager.settings import Settings

logger = get_logger()


def post_callback(settings: Settings, envelope: Envelope, callback_url: str) -> None:
    payload: Dict[str, Any] = envelope.model_dump()
    last_err = None
    attempts = max(1, settings.callback_retry_count)
    for index in range(attempts):
        try:
            with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
                response = client.post(callback_url, json=payload)
            logger.info(
                "callback HTTP %s attempt %s/%s",
                response.status_code,
                index + 1,
                attempts,
            )
            if response.status_code < 500:
                return
            last_err = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = redact(str(exc))
            logger.warning("callback failed: %s", last_err)
        time.sleep(min(2 ** index, 8))
    logger.error("callback gave up after %s attempts: %s", attempts, last_err)
