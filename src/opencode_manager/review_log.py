"""Creasy-style log helpers for the review path. OSM n8n logs stay on log.py."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional, Sequence

from opencode_manager.log import clip, log_command, log_command_result, redact

_USERINFO_RE = re.compile(r"(https?://)[^@\s\"'<>]+@", re.IGNORECASE)


def redact_userinfo(text: str) -> str:
    """Strip URL userinfo the Creasy way so notes never keep oauth2:token@."""
    if not text:
        return ""
    return _USERINFO_RE.sub(r"\1", redact(str(text)))


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if name:
        label = name if str(name).startswith("opencode_manager") else f"opencode_manager.{name}"
        return logging.getLogger(label)
    return logging.getLogger("opencode_manager")


def log_ok(logger: Any, headline: str, **fields: Any) -> None:
    bits = [f"{key}={clip(value, 500)}" for key, value in fields.items()]
    text = headline if str(headline).startswith("ok ") else f"ok {headline}"
    logger.info("%s%s", text, f" {' '.join(bits)}" if bits else "")


def log_fail(logger: Any, headline: str, **fields: Any) -> None:
    bits = [f"{key}={clip(value, 500)}" for key, value in fields.items()]
    text = headline if str(headline).startswith("FAIL") else f"FAIL {headline}"
    logger.error("%s%s", text, f" {' '.join(bits)}" if bits else "")


__all__ = [
    "clip",
    "get_logger",
    "log_command",
    "log_command_result",
    "log_fail",
    "log_ok",
    "redact",
    "redact_userinfo",
    "Sequence",
]
