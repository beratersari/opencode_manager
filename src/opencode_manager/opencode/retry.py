"""Outer retry + inner poll loop for one OpenCode job."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from opencode_manager.dashboard.store import JobStore
from opencode_manager.log import get_logger
from opencode_manager.models import AttemptRow, JobRecord, PromptRow, utc_now
from opencode_manager.opencode import prompts
from opencode_manager.opencode.serve import ServeHandle, start_serve, stop_serve
from opencode_manager.opencode.session import (
    OpenCodeClient,
    assess_idle,
    compact_marker_count,
    last_assistant_text,
    session_is_busy,
    session_is_compacting,
    snapshot_chat,
)
from opencode_manager.settings import Settings

logger = get_logger()


class AttemptFailed(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class JobFailed(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class LoopResult:
    text: str
    status_code: int
    error: Optional[str] = None


def _backoff(settings: Settings, attempt_index: int) -> None:
    delay = min(
        settings.retry_backoff_seconds * (2 ** max(0, attempt_index - 1)),
        settings.retry_backoff_cap_seconds,
    )
    if delay > 0:
        time.sleep(delay)


def _save(store: JobStore, job: JobRecord) -> None:
    store.save(job)


def run_opencode_job(
    job: JobRecord,
    *,
    settings: Settings,
    store: JobStore,
    clone: Path,
    should_stop: Callable[[], bool],
) -> LoopResult:
    attempts = max(1, job.retry_count)
    last_kind = "error"
    last_error = "OpenCode failed"
    handle: Optional[ServeHandle] = None
    client: Optional[OpenCodeClient] = None

    def close_serve() -> None:
        nonlocal handle, client
        if client is not None:
            if job.session_id:
                client.abort(job.session_id)
            client.close()
            client = None
        stop_serve(handle)
        handle = None
        job.serve_pid = None
        job.serve_port = None
        job.serve_base_url = ""
        _save(store, job)

    try:
        for attempt in range(1, attempts + 1):
            if should_stop():
                raise JobFailed(500, "manager shutting down")
            job.attempt = attempt
            _save(store, job)
            if attempt > 1:
                _backoff(settings, attempt)
            deadline = time.time() + job.timeout_in_seconds
            try:
                if handle is None:
                    remain = max(5.0, deadline - time.time())
                    handle = start_serve(
                        bin_name=settings.opencode_bin,
                        cwd=clone,
                        log_path=settings.job_log_dir / f"{job.job_id}.serve.log",
                        timeout=min(remain, 90.0),
                    )
                    job.serve_pid = handle.pid
                    job.serve_port = handle.port
                    job.serve_base_url = handle.base_url
                    _save(store, job)
                    client = OpenCodeClient(handle.base_url, str(clone))
                assert client is not None
                if not client.health():
                    raise AttemptFailed("serve-dead", "serve health failed")
                inbound = job.session_id or None
                had_live = bool(inbound and inbound.startswith("ses_"))
                try:
                    sid, created = client.resume_or_create(
                        inbound, title=f"{job.jira_id} {job.job_id}"
                    )
                except Exception as exc:  # noqa: BLE001
                    if had_live:
                        raise AttemptFailed("create-fail", f"resume rejected: {exc}") from exc
                    raise AttemptFailed("create-fail", f"session create failed: {exc}") from exc
                if had_live and created:
                    raise AttemptFailed("create-fail", "resume rejected; will not open a blank session")
                job.session_id = sid
                _save(store, job)
                if job.original_posted:
                    prompt_id, text = "HANG_RESUME", prompts.HANG_RESUME
                    if attempt > 1 and job.attempts and job.attempts[-1].kind == "incomplete":
                        prompt_id, text = "INCOMPLETE_RESUME", prompts.INCOMPLETE_RESUME
                else:
                    prompt_id, text = "ORIGINAL", job.prompt
                _post_user(job, client, store, prompt_id, text)
                outcome = _inner_loop(
                    job,
                    client,
                    store,
                    settings=settings,
                    deadline=deadline,
                    should_stop=should_stop,
                )
                if outcome == "success":
                    return LoopResult(text=job.text or last_assistant_text(client.list_messages(job.session_id)), status_code=200)
                if outcome == "asking":
                    raise JobFailed(500, "model still asking after UNATTENDED_NUDGE")
                if outcome == "compact_leftover":
                    raise JobFailed(500, "compact leftover after COMPACT_LOOP_NUDGE")
                last_kind = outcome
                last_error = f"attempt {attempt} ended: {outcome}"
                job.attempts.append(
                    AttemptRow(
                        number=attempt,
                        kind=outcome,
                        prompt_id=prompt_id,
                        session_id=job.session_id,
                        error=last_error,
                        ended_at=utc_now(),
                    )
                )
                _save(store, job)
                if outcome == "timeout":
                    close_serve()
                    continue
                if outcome in {"hang", "serve-dead"}:
                    close_serve()
                    continue
                # incomplete: same serve
                continue
            except AttemptFailed as exc:
                last_kind = exc.kind
                last_error = exc.message
                logger.error("attempt %s failed: %s", attempt, exc.message)
                job.attempts.append(
                    AttemptRow(
                        number=attempt,
                        kind=exc.kind,
                        prompt_id="HANG_RESUME" if job.original_posted else "ORIGINAL",
                        session_id=job.session_id,
                        error=exc.message,
                        ended_at=utc_now(),
                    )
                )
                _save(store, job)
                close_serve()
                continue
        status = 504 if last_kind == "timeout" else 500
        raise JobFailed(status, last_error)
    finally:
        close_serve()


def _post_user(
    job: JobRecord,
    client: OpenCodeClient,
    store: JobStore,
    prompt_id: str,
    text: str,
) -> None:
    status = client.status()
    if session_is_busy(status, job.session_id):
        raise AttemptFailed("hang", "session busy; refusing to POST a user message")
    client.post_message(job.session_id, text, model=job.model, agent=job.agent_mode)
    job.prompts.append(PromptRow(id=prompt_id, text=text, posted_at=utc_now()))
    if prompt_id == "ORIGINAL":
        job.original_posted = True
    _save(store, job)
    logger.info("posted %s", prompt_id)


def _inner_loop(
    job: JobRecord,
    client: OpenCodeClient,
    store: JobStore,
    *,
    settings: Settings,
    deadline: float,
    should_stop: Callable[[], bool],
) -> str:
    nudged = any(row.id == "UNATTENDED_NUDGE" for row in job.prompts)
    compact_nudged = False
    last_progress = time.time()
    last_msg_n = 0
    last_compact_n = 0
    hang_started: Optional[float] = None
    awaiting_turn = True

    while True:
        if should_stop():
            raise JobFailed(500, "manager shutting down")
        if time.time() >= deadline:
            if job.session_id:
                client.abort(job.session_id)
            return "timeout"
        if not client.health():
            return "serve-dead"
        status = client.status()
        busy = session_is_busy(status, job.session_id)
        compacting = session_is_compacting(status, job.session_id)
        try:
            messages = client.list_messages(job.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list messages failed: %s", exc)
            messages = []
        job.chat_snapshot = snapshot_chat(messages, job.session_id)
        text = last_assistant_text(messages)
        if text:
            job.text = text
        _save(store, job)

        msg_n = len(messages)
        compact_n = compact_marker_count(messages)
        if msg_n != last_msg_n or compact_n != last_compact_n or compacting:
            last_progress = time.time()
            if msg_n > last_msg_n and text:
                awaiting_turn = False
            last_msg_n = msg_n
            last_compact_n = compact_n
            hang_started = None

        if compacting:
            awaiting_turn = False
            time.sleep(1.0)
            continue

        if busy:
            awaiting_turn = False
            if hang_started is None:
                hang_started = time.time()
            if time.time() - last_progress >= settings.hang_timeout_seconds:
                client.abort(job.session_id)
                return "hang"
            time.sleep(1.0)
            continue

        # idle — after a POST, OpenCode may not flip busy for a beat.
        if awaiting_turn:
            time.sleep(0.4)
            continue

        verdict = assess_idle(messages)
        if compact_n >= 8 and verdict != "success" and not compact_nudged:
            client.abort(job.session_id)
            wait_idle = time.time() + 60
            while time.time() < wait_idle and session_is_busy(client.status(), job.session_id):
                time.sleep(0.5)
            _post_user(job, client, store, "COMPACT_LOOP_NUDGE", prompts.COMPACT_LOOP_NUDGE)
            compact_nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        if verdict == "success":
            return "success"
        if verdict == "question":
            if nudged:
                return "asking"
            _post_user(job, client, store, "UNATTENDED_NUDGE", prompts.UNATTENDED_NUDGE)
            nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        if verdict == "compact_leftover":
            if compact_nudged:
                return "compact_leftover"
            _post_user(job, client, store, "COMPACT_LOOP_NUDGE", prompts.COMPACT_LOOP_NUDGE)
            compact_nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        return "incomplete"
