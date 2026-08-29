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
from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.opencode.serve import ServeHandle, start_serve, stop_serve
from opencode_manager.opencode.session import (
    OpenCodeClient,
    assess_idle,
    compact_marker_count,
    last_assistant_id,
    last_assistant_text,
    turn_has_new_assistant,
    session_is_busy,
    session_is_compacting,
    snapshot_chat,
)
from opencode_manager.settings import Settings

logger = get_logger()
_REPEAT_LOG_EVERY = 50


def _log_every(n: int, every: int = _REPEAT_LOG_EVERY) -> bool:
    """True on the first event and every `every` repeats after that."""
    return n == 1 or (n > 0 and n % every == 0)


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

    def record_spawn(spawned: ServeHandle) -> None:
        job.serve_pid = spawned.pid
        job.serve_port = spawned.port
        job.serve_base_url = spawned.base_url
        _save(store, job)

    def close_serve() -> None:
        nonlocal handle, client
        logger.info("close serve session=%s pid=%s", job.session_id or "", handle.pid if handle else job.serve_pid)
        if client is not None:
            if job.session_id:
                logger.info("abort session %s", job.session_id)
                client.abort(job.session_id)
            client.close()
            client = None
        stop_serve(handle)
        if handle is None and job.serve_pid:
            kill_pid(job.serve_pid)
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
            logger.info(
                "attempt %s/%s start original_posted=%s session=%s timeout=%ss",
                attempt,
                attempts,
                job.original_posted,
                job.session_id or "(none)",
                job.timeout_in_seconds,
            )
            if attempt > 1:
                logger.info("outer backoff before attempt %s", attempt)
                _backoff(settings, attempt)
            deadline = time.time() + job.timeout_in_seconds
            try:
                if handle is None:
                    remain = max(5.0, deadline - time.time())
                    try:
                        handle = start_serve(
                            bin_name=settings.opencode_bin,
                            cwd=clone,
                            log_path=settings.work_dir / ".serve" / f"{job.job_id}.log",
                            timeout=min(remain, 90.0),
                            on_spawn=record_spawn,
                            should_stop=should_stop,
                            attempt_timeout=float(job.timeout_in_seconds),
                            hang_timeout=float(settings.hang_timeout_seconds),
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise AttemptFailed("serve-dead", f"serve boot failed: {exc}") from exc
                    client = OpenCodeClient(handle.base_url, str(clone))
                assert client is not None
                if not client.health():
                    raise AttemptFailed("serve-dead", "serve health failed")
                inbound = job.session_id or None
                already_bound = bool(job.session_bound)
                try:
                    sid, created = client.resume_or_create(
                        inbound, title=f"{job.jira_id} {job.job_id}"
                    )
                except Exception as exc:  # noqa: BLE001
                    if already_bound:
                        raise AttemptFailed("create-fail", f"resume rejected: {exc}") from exc
                    raise AttemptFailed("create-fail", f"session create failed: {exc}") from exc
                if already_bound and created:
                    raise AttemptFailed(
                        "create-fail",
                        "resume rejected; will not open a blank session",
                    )
                logger.info(
                    "session %s id=%s inbound=%s already_bound=%s",
                    "created" if created else "resumed",
                    sid,
                    inbound or "(none)",
                    already_bound,
                )
                job.session_id = sid
                job.session_bound = True
                _save(store, job)
                if job.original_posted:
                    prompt_id, text = "HANG_RESUME", prompts.HANG_RESUME
                    if attempt > 1 and job.attempts and job.attempts[-1].kind == "incomplete":
                        prompt_id, text = "INCOMPLETE_RESUME", prompts.INCOMPLETE_RESUME
                else:
                    prompt_id, text = "ORIGINAL", job.prompt
                try:
                    prior = client.list_messages(job.session_id)
                except Exception:
                    prior = []
                baseline_assistant = last_assistant_id(prior)
                logger.info(
                    "turn baseline messages=%s last_assistant=%s",
                    len(prior),
                    baseline_assistant or "(none)",
                )
                _post_user(job, client, store, prompt_id, text)
                outcome = _inner_loop(
                    job,
                    client,
                    store,
                    settings=settings,
                    deadline=deadline,
                    should_stop=should_stop,
                    baseline_assistant_id=baseline_assistant,
                    baseline_n=len(prior),
                )
                logger.info("inner loop left attempt=%s outcome=%s", attempt, outcome)
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
    logger.info("POST user message prompt_id=%s chars=%s model=%s agent=%s", prompt_id, len(text), job.model, job.agent_mode)
    try:
        client.post_message(job.session_id, text, model=job.model, agent=job.agent_mode)
    except Exception as exc:  # noqa: BLE001
        logger.error("user message POST failed prompt_id=%s err=%s", prompt_id, exc)
        raise AttemptFailed("transport", f"user message POST failed: {exc}") from exc
    job.prompts.append(PromptRow(id=prompt_id, text=text, posted_at=utc_now()))
    if prompt_id == "ORIGINAL":
        job.original_posted = True
        logger.info("ORIGINAL marked posted (OpenCode accepted the POST)")
    _save(store, job)
    logger.info("posted %s ok", prompt_id)


def _inner_loop(
    job: JobRecord,
    client: OpenCodeClient,
    store: JobStore,
    *,
    settings: Settings,
    deadline: float,
    should_stop: Callable[[], bool],
    baseline_assistant_id: str = "",
    baseline_n: int = 0,
) -> str:
    nudged = any(row.id == "UNATTENDED_NUDGE" for row in job.prompts)
    compact_nudged = False
    last_progress = time.time()
    last_msg_n = baseline_n
    last_compact_n = 0
    hang_started: Optional[float] = None
    awaiting_turn = True
    last_phase = ""
    new_assistant_logs = 0
    hang_clock_logs = 0
    logger.info("inner loop enter hang_timeout=%ss", settings.hang_timeout_seconds)

    while True:
        if should_stop():
            logger.warning("inner loop stop: manager shutting down")
            raise JobFailed(500, "manager shutting down")
        if time.time() >= deadline:
            logger.warning("inner loop attempt clock hit zero")
            if job.session_id:
                client.abort(job.session_id)
            return "timeout"
        if not client.health():
            logger.warning("inner loop serve health failed")
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
        new_assistant = turn_has_new_assistant(messages, baseline_assistant_id)
        if new_assistant and text:
            job.text = text
        _save(store, job)

        msg_n = len(messages)
        compact_n = compact_marker_count(messages)
        if msg_n != last_msg_n or compact_n != last_compact_n or compacting or new_assistant:
            last_progress = time.time()
            last_msg_n = msg_n
            last_compact_n = compact_n
            hang_started = None
        if new_assistant:
            awaiting_turn = False
            new_assistant_logs += 1
            if _log_every(new_assistant_logs):
                logger.info(
                    "new assistant after this turn id=%s poll=%s",
                    last_assistant_id(messages),
                    new_assistant_logs,
                )

        phase = (
            "compacting"
            if compacting
            else "busy"
            if busy
            else "awaiting"
            if awaiting_turn
            else "idle"
        )
        if phase != last_phase:
            logger.info(
                "session phase=%s messages=%s compact_markers=%s",
                phase,
                msg_n,
                compact_n,
            )
            last_phase = phase

        if compacting:
            awaiting_turn = False
            time.sleep(1.0)
            continue

        if busy:
            awaiting_turn = False
            if hang_started is None:
                hang_started = time.time()
                hang_clock_logs += 1
                if _log_every(hang_clock_logs):
                    logger.info(
                        "hang clock started (busy, not compacting) poll=%s",
                        hang_clock_logs,
                    )
            if time.time() - last_progress >= settings.hang_timeout_seconds:
                logger.warning("hang watchdog fired after %ss with no new markers", settings.hang_timeout_seconds)
                client.abort(job.session_id)
                return "hang"
            time.sleep(1.0)
            continue

        # idle — after a POST, OpenCode may not flip busy for a beat.
        if awaiting_turn:
            time.sleep(0.4)
            continue

        verdict = assess_idle(messages)
        logger.info("session idle assess=%s messages=%s", verdict, msg_n)
        if compact_n >= 8 and verdict != "success" and not compact_nudged:
            client.abort(job.session_id)
            wait_idle = time.time() + 60
            while time.time() < wait_idle and session_is_busy(client.status(), job.session_id):
                time.sleep(0.5)
            baseline_assistant_id = last_assistant_id(messages)
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
            baseline_assistant_id = last_assistant_id(messages)
            _post_user(job, client, store, "UNATTENDED_NUDGE", prompts.UNATTENDED_NUDGE)
            nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        if verdict == "compact_leftover":
            if compact_nudged:
                return "compact_leftover"
            baseline_assistant_id = last_assistant_id(messages)
            _post_user(job, client, store, "COMPACT_LOOP_NUDGE", prompts.COMPACT_LOOP_NUDGE)
            compact_nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        return "incomplete"
