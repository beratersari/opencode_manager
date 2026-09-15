"""Safe diagnostic snapshots for job logs, app.log, and dashboard reports."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from opencode_manager.review_log import get_logger, redact_userinfo

logger = get_logger("diag")

_DROP_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "pat",
        "authorization",
        "api_key",
        "apikey",
        "gitlab_token",
        "azure_token",
        "webhook_secret",
        "dashboard_token",
        "dashboard_password",
        "azure_webhook_password",
        "creasy_git_token",
    }
)


def safe_url(url: str) -> str:
    """Host + path only. No userinfo, query, or fragment."""
    text = redact_userinfo(str(url or "").strip())
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "", "", "", ""))


def safe_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in fields.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        lowered = key.lower().replace("-", "_")
        if lowered in _DROP_KEYS or lowered.endswith("_token") or lowered.endswith("_password"):
            if lowered.endswith("_set") or lowered.endswith("_chars"):
                out[key] = value
            else:
                out[f"{key}_set"] = bool(value)
            continue
        if isinstance(value, str) and ("url" in lowered or lowered in {"clone", "remote", "origin"}):
            out[key] = safe_url(value) or value
        else:
            out[key] = value
    return out


def log_diag(scope: str, stage: str, **fields: Any) -> None:
    """Greppable line: ``diag job stage=clone …`` or ``diag system stage=start …``."""
    bits = [f"diag {scope}", f"stage={stage}"]
    for key, value in safe_fields(fields).items():
        bits.append(f"{key}={redact_userinfo('' if value is None else str(value))}")
    logger.info(" ".join(bits))


def merge_job_diag(job: Any, **fields: Any) -> dict[str, Any]:
    current = dict(getattr(job, "diagnostics", None) or {})
    current.update(safe_fields(fields))
    job.diagnostics = current
    return current
