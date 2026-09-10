"""Per-job log context via contextvars."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_job_id: ContextVar[Optional[str]] = ContextVar("osm_job_id", default=None)
_jira_id: ContextVar[Optional[str]] = ContextVar("osm_jira_id", default=None)
_log_file: ContextVar[Optional[str]] = ContextVar("osm_log_file", default=None)


def get_job_id() -> Optional[str]:
    return _job_id.get()


def get_jira_id() -> Optional[str]:
    return _jira_id.get()


def get_log_file() -> Optional[str]:
    return _log_file.get()


def set_job_id(job_id: Optional[str]) -> None:
    _job_id.set((job_id or "").strip() or None)


def set_jira_id(jira_id: Optional[str]) -> None:
    _jira_id.set((jira_id or "").strip() or None)


def set_log_file(name: Optional[str]) -> None:
    _log_file.set((name or "").strip() or None)


def bind(
    job_id: Optional[str] = None,
    jira_id: Optional[str] = None,
    log_file: Optional[str] = None,
    mr_key: Optional[str] = None,
) -> None:
    if job_id is not None:
        set_job_id(job_id)
    key = jira_id if jira_id is not None else mr_key
    if key is not None:
        set_jira_id(key)
    if log_file is not None:
        set_log_file(log_file)


def get_mr_key() -> Optional[str]:
    return get_jira_id()


def clear() -> None:
    _job_id.set(None)
    _jira_id.set(None)
    _log_file.set(None)


@contextmanager
def bound(
    job_id: Optional[str] = None,
    mr_key: Optional[str] = None,
    log_file: Optional[str] = None,
) -> Iterator[None]:
    """Review-path bind that always clears on exit."""
    bind(job_id=job_id, jira_id=mr_key, log_file=log_file)
    try:
        yield
    finally:
        clear()
