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
LIVE_STATUSES = frozenset({"queued", "running"})
ERROR_STATUSES = frozenset({"error", "timeout", "not_found"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mint_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:16]


class JobRequest(BaseModel):
    repo_url: str
    PAT: str
    source_branch: str
    session_id: Optional[str] = None
    prompt: str
    model: str
    agent_mode: str
    timeout_in_seconds: int
    retry_count: int
    jira_id: str
    callback_url: str

    @field_validator("repo_url", "PAT", "source_branch", "prompt", "model", "agent_mode", "jira_id", "callback_url")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

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
    prompt: str = ""
    attempts: List[AttemptRow] = Field(default_factory=list)
    prompts: List[PromptRow] = Field(default_factory=list)
    chat_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    extra_pids: List[int] = Field(default_factory=list)

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
        "PAT",
        "source_branch",
        "prompt",
        "model",
        "agent_mode",
        "timeout_in_seconds",
        "retry_count",
        "jira_id",
        "callback_url",
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
    return None


def parse_model(model: str) -> tuple[str, str]:
    provider, _, name = model.strip().partition("/")
    return provider, name


def callback_host_allowed(callback_url: str, allowed: List[str]) -> bool:
    if not allowed:
        return True
    host = (urlparse(callback_url).hostname or "").lower()
    return host in allowed
