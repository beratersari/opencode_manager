"""Request, response, and job-history records."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

KNOWN_AGENTS = frozenset({"build", "plan", "general", "explore"})
_MODEL_RE = re.compile(r"^[^/\s]+/[^/\s].*$")
# Ticket folder. Must be a strict child of work_dir — not ".", "..", or slashes.
_JIRA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
LIVE_STATUSES = frozenset({"queued", "running"})
ERROR_STATUSES = frozenset({"error", "timeout", "not_found"})
LIST_FILTERS = frozenset({"all", "active", "error", "completed"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mint_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:16]


class JobRequest(BaseModel):
    repo_url: str
    PAT: str = ""
    source_branch: str
    session_id: Optional[str] = None
    prompt: str
    model: str
    agent_mode: str
    timeout_in_seconds: int
    retry_count: int
    jira_id: str
    callback_url: str = ""

    @field_validator("repo_url", "source_branch", "prompt", "model", "agent_mode", "jira_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("PAT")
    @classmethod
    def _optional_pat(cls, value: Optional[str]) -> str:
        return (value or "").strip()

    @field_validator("callback_url")
    @classmethod
    def _optional_callback(cls, value: Optional[str]) -> str:
        return (value or "").strip()

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("jira_id")
    @classmethod
    def _jira(cls, value: str) -> str:
        text = (value or "").strip()
        if not _JIRA_ID_RE.match(text):
            raise ValueError("jira_id must be a Windows-safe ticket id")
        return text

    @field_validator("retry_count")
    @classmethod
    def _retries(cls, value: int) -> int:
        return 1 if int(value) < 1 else int(value)

    @field_validator("timeout_in_seconds")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("timeout_in_seconds must be >= 1")
        return int(value)


class Envelope(BaseModel):
    text: str
    session_id: str = ""
    status_code: int
    jira_id: str
    job_id: str


def poll_payload(job: "JobRecord") -> tuple[int, Dict[str, Any]]:
    """HTTP status + envelope for GET /jobs/{id}. Extra live/status for the poller."""
    live = bool(job.live or job.status in LIVE_STATUSES)
    if live:
        return 202, {
            "text": job.text or "Job is still in progress.",
            "session_id": job.session_id or "",
            "status_code": 202,
            "jira_id": job.jira_id,
            "job_id": job.job_id,
            "live": True,
            "status": job.status,
        }
    if job.callback_status_code is not None:
        code = int(job.callback_status_code)
    elif job.status == "success":
        code = 200
    elif job.status == "not_found":
        code = 404
    elif job.status == "timeout":
        code = 504
    else:
        code = 500
    return 200, {
        "text": job.text or job.error_message or "",
        "session_id": job.session_id or "",
        "status_code": code,
        "jira_id": job.jira_id,
        "job_id": job.job_id,
        "live": False,
        "status": job.status,
    }


class AttemptRow(BaseModel):
    number: int
    kind: str
    prompt_id: str = ""
    session_id: str = ""
    error: Optional[str] = None
    ended_at: Optional[str] = None


class PromptRow(BaseModel):
    id: str
    text: str
    posted_at: str


class JobRecord(BaseModel):
    job_id: str
    jira_id: str
    status: str = "queued"
    live: bool = True
    agent_mode: str = ""
    model: str = ""
    session_id: str = ""
    repo_url: str = ""
    source_branch: str = ""
    clone_path: str = ""
    serve_pid: Optional[int] = None
    serve_port: Optional[int] = None
    serve_base_url: str = ""
    timeout_in_seconds: int = 1800
    retry_count: int = 1
    attempt: int = 1
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None
    accepted_at: Optional[str] = None
    error_message: Optional[str] = None
    callback_status_code: Optional[int] = None
    callback_url: str = ""
    text: str = ""
    original_posted: bool = False
    session_bound: bool = False
    prompt: str = ""
    attempts: List[AttemptRow] = Field(default_factory=list)
    prompts: List[PromptRow] = Field(default_factory=list)
    chat_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    extra_pids: List[int] = Field(default_factory=list)
    log_file: str = ""

    def public_dict(self) -> Dict[str, Any]:
        data = self.model_dump()
        data.pop("callback_url", None)
        data.pop("prompt", None)
        data.pop("extra_pids", None)
        return data


def validate_request_fields(body: Dict[str, Any]) -> Optional[str]:
    """Return a 400 message or None if the body is usable."""
    required = (
        "repo_url",
        "source_branch",
        "prompt",
        "model",
        "agent_mode",
        "timeout_in_seconds",
        "retry_count",
        "jira_id",
    )
    for key in required:
        if key not in body or body[key] is None or str(body[key]).strip() == "":
            return f"missing required field: {key}"
    repo = str(body["repo_url"]).strip()
    lowered = repo.lower()
    if lowered.startswith("git@") or lowered.startswith("ssh://"):
        return "SSH repo_url is rejected"
    parsed = urlparse(repo)
    if parsed.scheme not in {"http", "https", "file"}:
        return "repo_url must be http(s) or file"
    model = str(body["model"]).strip()
    if not _MODEL_RE.match(model) or model.count("/") < 1:
        provider, _, name = model.partition("/")
        if not provider or not name:
            return "model must be provider/id"
    agent = str(body["agent_mode"]).strip()
    if agent not in KNOWN_AGENTS:
        return f"unknown agent_mode: {agent}"
    if "callback_url" in body and body["callback_url"] is not None and str(body["callback_url"]).strip():
        callback = str(body["callback_url"]).strip()
        cb = urlparse(callback)
        if cb.scheme not in {"http", "https"} or not cb.netloc:
            return "callback_url must be an absolute http(s) URL"
    try:
        int(body["timeout_in_seconds"])
        int(body["retry_count"])
    except (TypeError, ValueError):
        return "timeout_in_seconds and retry_count must be integers"
    if int(body["timeout_in_seconds"]) < 1:
        return "timeout_in_seconds must be >= 1"
    if not _JIRA_ID_RE.match(str(body["jira_id"]).strip()):
        return "jira_id must be a Windows-safe ticket id"
    return None


def validate_session_delete_fields(body: Dict[str, Any]) -> Optional[str]:
    """Return a 400 message or None if DELETE /sessions body is usable."""
    if not isinstance(body, dict):
        return "missing required field: jira_id"
    for key in ("jira_id", "session_id"):
        if key not in body or body[key] is None or str(body[key]).strip() == "":
            return f"missing required field: {key}"
    if not _JIRA_ID_RE.match(str(body["jira_id"]).strip()):
        return "jira_id must be a Windows-safe ticket id"
    session_id = str(body["session_id"]).strip()
    if session_id == "-1" or not session_id.startswith("ses_"):
        return "session_id must be a live OpenCode ses_* id"
    return None


def parse_model(model: str) -> tuple[str, str]:
    provider, _, name = model.strip().partition("/")
    return provider, name


def job_matches_list_filter(job: "JobRecord", filt: str) -> bool:
    """Dashboard list filter. `queue` is GET /api/queue, not this helper."""
    key = (filt or "all").strip().lower()
    status = (job.status or "").lower()
    if key in {"", "all"}:
        return True
    if key == "active":
        return status == "running" or bool(job.live and status != "queued")
    if key == "error":
        return status in ERROR_STATUSES
    if key == "completed":
        return status == "success"
    return True


_ALLOW_ALL_HOSTS = frozenset({"*", "all"})


def callback_host_allowed(callback_url: str, allowed: List[str]) -> bool:
    """Empty list, `*`, or `all` accepts every http(s) callback_url host."""
    tokens = {str(h).strip().lower() for h in allowed if str(h).strip()}
    if not tokens or tokens & _ALLOW_ALL_HOSTS:
        return True
    host = (urlparse(callback_url).hostname or "").lower()
    if host in tokens:
        return True
    for token in tokens:
        if token.startswith("*.") and len(token) > 2:
            suffix = token[2:]
            if host == suffix or host.endswith("." + suffix):
                return True
    return False
