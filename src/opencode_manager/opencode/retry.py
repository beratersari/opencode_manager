"""Outer retry + inner poll loop for one OpenCode job."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from opencode_manager.dashboard.store import JobStore, persist_job
from opencode_manager.log import clip, get_logger, log_fail
from opencode_manager.models import AttemptRow, JobRecord, PromptRow, utc_now, usable_session_id
from opencode_manager.opencode import prompts
from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.opencode.serve import ServeHandle, serve_log_path, start_serve, stop_serve
from opencode_manager.opencode.session import (
    OpenCodeClient,
    assess_idle,
    compact_marker_count,
    last_assistant_id,
    last_assistant_text,
    model_is_known,
    turn_has_new_assistant,
    session_is_busy,
    session_is_compacting,
    snapshot_chat,
    unknown_model_message,
    looks_like_unknown_model_error,
    _last_finish,
)
from opencode_manager.settings import Settings

logger = get_logger()
_REPEAT_LOG_EVERY = 50
# Compact-loop is ~8 *new* markers this wait (PLAN §5.3). Lifetime history
# from a resumed ses_* must not count (virtual_developer KAN-95).
_COMPACT_LOOP_NEW = 8


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
    """History write must not kill a live OpenCode turn (Windows file lock)."""
    persist_job(store, job)


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
        try:
            logger.info(
                "close serve session=%s pid=%s",
                job.session_id or "",
                handle.pid if handle else job.serve_pid,
            )
            if client is not None:
                if usable_session_id(job.session_id):
                    logger.info("abort session %s", job.session_id)
                    try:
                        client.abort(job.session_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("abort during close_serve failed")
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    logger.exception("client close during close_serve failed")
                client = None
            try:
                stop_serve(handle)
            except Exception:  # noqa: BLE001
                logger.exception("stop_serve during close_serve failed")
            if handle is None and job.serve_pid:
                try:
                    kill_pid(job.serve_pid)
                except Exception:  # noqa: BLE001
                    logger.exception("kill leftover serve_pid failed")
            handle = None
            job.serve_pid = None
            job.serve_port = None
            job.serve_base_url = ""
            _save(store, job)
        except Exception:  # noqa: BLE001
            logger.exception("close_serve failed job=%s", job.job_id)

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
                            log_path=serve_log_path(settings.serve_dir, job.job_id),
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
                remain = max(5.0, deadline - time.time())
                wait_directory = getattr(client, "wait_directory", None)
                if callable(wait_directory):
                    try:
                        wait_directory(timeout=remain, should_stop=should_stop)
                    except TypeError:
                        wait_directory(timeout=remain)
                    except RuntimeError as exc:
                        if "shutting down" in str(exc).lower():
                            raise JobFailed(500, "manager shutting down") from exc
                        raise AttemptFailed(
                            "serve-dead",
                            f"directory instance not ready: {exc}",
                        ) from exc
                    except Exception as exc:  # noqa: BLE001
                        raise AttemptFailed(
                            "serve-dead",
                            f"directory instance not ready: {exc}",
                        ) from exc
                known_models: list[str] = []
                inventory_ok = False
                list_models = getattr(client, "list_known_models", None)
                if callable(list_models):
                    try:
                        remain = max(5.0, deadline - time.time())
                        try:
                            known_models = list_models(timeout=min(60.0, remain))
                        except TypeError:
                            known_models = list_models()
                        inventory_ok = True
                    except Exception as exc:  # noqa: BLE001
                        logger.info("model list check skipped: %s", exc)
                if inventory_ok and (
                    not known_models or not model_is_known(job.model, known_models)
                ):
                    raise JobFailed(500, unknown_model_message(job.model, known_models))
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
                    compact_floor: Optional[int] = compact_marker_count(prior)
                except Exception:
                    prior = []
                    compact_floor = None
                baseline_assistant = last_assistant_id(prior)
                logger.info(
                    "turn baseline messages=%s last_assistant=%s compact_markers=%s",
                    len(prior),
                    baseline_assistant or "(none)",
                    compact_floor if compact_floor is not None else "(unknown)",
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
                    baseline_compact_n=compact_floor,
                )
                logger.info(
                    "inner loop left attempt=%s/%s outcome=%s remaining=%s original_posted=%s session=%s",
                    attempt,
                    attempts,
                    outcome,
                    attempts - attempt,
                    job.original_posted,
                    job.session_id or "(none)",
                )
                if outcome == "success":
                    return LoopResult(text=job.text or last_assistant_text(client.list_messages(job.session_id)), status_code=200)
                if outcome == "asking":
                    log_fail(
                        logger,
                        "job fail still asking after UNATTENDED_NUDGE",
                        attempt=attempt,
                        session=job.session_id,
                        text=job.text,
                    )
                    raise JobFailed(500, "model still asking after UNATTENDED_NUDGE")
                if outcome == "compact_leftover":
                    log_fail(
                        logger,
                        "job fail compact leftover after COMPACT_LOOP_NUDGE",
                        attempt=attempt,
                        session=job.session_id,
                    )
                    raise JobFailed(500, "compact leftover after COMPACT_LOOP_NUDGE")
                last_kind = outcome
                last_error = f"attempt {attempt} ended: {outcome}"
                logger.info(
                    "attempt %s/%s will retry kind=%s next_prompt=%s kill_serve=%s",
                    attempt,
                    attempts,
                    outcome,
                    "INCOMPLETE_RESUME"
                    if outcome == "incomplete"
                    else ("HANG_RESUME" if job.original_posted else "ORIGINAL"),
                    outcome != "incomplete",
                )
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
                log_fail(
                    logger,
                    "attempt failed",
                    attempt=f"{attempt}/{attempts}",
                    kind=exc.kind,
                    err=exc.message,
                    session=job.session_id,
                    original_posted=job.original_posted,
                )
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
        log_fail(
            logger,
            "OpenCode attempts exhausted",
            last_kind=last_kind,
            last_error=last_error,
            attempts=attempts,
            callback_status=status,
            session=job.session_id,
        )
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
        log_fail(
            logger,
            "refuse POST user message; session busy",
            prompt_id=prompt_id,
            session=job.session_id,
            status=status,
        )
        raise AttemptFailed("hang", "session busy; refusing to POST a user message")
    logger.info(
        "POST user message prompt_id=%s chars=%s model=%s agent=%s session=%s preview=%s",
        prompt_id,
        len(text),
        job.model,
        job.agent_mode,
        job.session_id,
        clip(text, 160),
    )
    try:
        client.post_message(job.session_id, text, model=job.model, agent=job.agent_mode)
    except Exception as exc:  # noqa: BLE001
        logger.error("user message POST failed prompt_id=%s err=%s", prompt_id, exc)
        if looks_like_unknown_model_error(str(exc), job.model):
            raise JobFailed(500, unknown_model_message(job.model, [])) from exc
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
    baseline_compact_n: Optional[int] = None,
) -> str:
    nudged = any(row.id == "UNATTENDED_NUDGE" for row in job.prompts)
    compact_nudged = False
    last_progress = time.time()
    last_msg_n = baseline_n
    compact_floor = baseline_compact_n
    last_compact_n = baseline_compact_n if baseline_compact_n is not None else 0
    hang_started: Optional[float] = None
    answered_this_turn = False
    awaiting_turn = True
    last_phase = ""
    new_assistant_logs = 0
    hang_clock_logs = 0
    logger.info(
        "inner loop enter hang_timeout=%ss attempt_deadline_in=%ss session=%s",
        settings.hang_timeout_seconds,
        max(0.0, deadline - time.time()),
        job.session_id or "(none)",
    )

    def _leave(kind: str, messages: list, **extra: object) -> str:
        role, finish = _last_finish(messages)
        logger.info(
            "inner leave kind=%s last_role=%s last_finish=%s last_assistant=%s "
            "messages=%s text=%s %s",
            kind,
            role or "-",
            finish or "(none)",
            last_assistant_id(messages) or "(none)",
            len(messages),
            clip(last_assistant_text(messages), 240),
            " ".join(f"{k}={v}" for k, v in extra.items()),
        )
        return kind

    while True:
        if should_stop():
            logger.warning("inner loop stop: manager shutting down")
            raise JobFailed(500, "manager shutting down")
        if time.time() >= deadline:
            logger.warning("inner loop attempt clock hit zero")
            if job.session_id:
                client.abort(job.session_id)
            try:
                timed = client.list_messages(job.session_id) if job.session_id else []
            except Exception:
                timed = []
            return _leave("timeout", timed)
        if not client.health():
            logger.warning("inner loop serve health failed")
            try:
                dead_msgs = client.list_messages(job.session_id) if job.session_id else []
            except Exception:
                dead_msgs = []
            return _leave("serve-dead", dead_msgs)
        status = client.status()
        busy = session_is_busy(status, job.session_id)
        session_info = None
        if job.session_id and hasattr(client, "session_payload"):
            try:
                session_info = client.session_payload(job.session_id)
            except Exception:
                session_info = None
        compacting = session_is_compacting(
            status, job.session_id, session_info=session_info
        )
        listed_ok = True
        try:
            messages = client.list_messages(job.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list messages failed: %s", exc)
            messages = []
            listed_ok = False
        job.chat_snapshot = snapshot_chat(messages, job.session_id)
        if looks_like_unknown_model_error(str(messages)):
            raise JobFailed(500, unknown_model_message(job.model, []))
        text = last_assistant_text(messages)
        new_assistant = False
        if listed_ok:
            new_assistant = turn_has_new_assistant(messages, baseline_assistant_id)
            if new_assistant:
                answered_this_turn = True
        elif answered_this_turn:
            new_assistant = True
        if new_assistant and text:
            job.text = text
        _save(store, job)

        msg_n = len(messages)
        compact_n = compact_marker_count(messages)
        if compact_floor is None and listed_ok:
            compact_floor = compact_n
            last_compact_n = compact_n
        new_compacts = compact_n - (compact_floor if compact_floor is not None else 0)
        # INTENTIONAL: a new assistant this turn (id ≠ baseline) is progress
        # for the rest of the wait. Hang is "never started answering".
        if listed_ok and (
            msg_n != last_msg_n or compact_n != last_compact_n or compacting or new_assistant
        ):
            last_progress = time.time()
            last_msg_n = msg_n
            last_compact_n = compact_n
            hang_started = None
        elif not listed_ok and (answered_this_turn or compacting):
            last_progress = time.time()
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
                "session phase=%s messages=%s compact_markers=%s new_compacts=%s",
                phase,
                msg_n,
                compact_n,
                new_compacts,
            )
            last_phase = phase

        if compacting:
            awaiting_turn = False
            time.sleep(1.0)
            continue

        if busy:
            awaiting_turn = False
            # Mid-generation (assistant already this turn) is the attempt
            # clock, not hang. Latch survives a later list_messages failure.
            if answered_this_turn or new_assistant:
                hang_started = None
                time.sleep(1.0)
                continue
            if hang_started is None:
                hang_started = time.time()
                hang_clock_logs += 1
                if _log_every(hang_clock_logs):
                    logger.info(
                        "hang clock started (busy, not compacting) poll=%s",
                        hang_clock_logs,
                    )
            if time.time() - last_progress >= settings.hang_timeout_seconds:
                logger.warning(
                    "hang watchdog fired after %ss with no new markers last_assistant=%s last_finish=%s messages=%s",
                    settings.hang_timeout_seconds,
                    last_assistant_id(messages) or "(none)",
                    _last_finish(messages)[1] or "(none)",
                    msg_n,
                )
                client.abort(job.session_id)
                return _leave("hang", messages, quiet_for=f"{settings.hang_timeout_seconds}s")
            time.sleep(1.0)
            continue

        # idle — after a POST, OpenCode may not flip busy for a beat.
        if awaiting_turn:
            time.sleep(0.4)
            continue

        verdict = assess_idle(messages)
        role, finish = _last_finish(messages)
        logger.info(
            "session idle assess=%s messages=%s new_compacts=%s last_role=%s last_finish=%s last_assistant=%s",
            verdict,
            msg_n,
            new_compacts,
            role or "-",
            finish or "(none)",
            last_assistant_id(messages) or "(none)",
        )
        if new_compacts >= _COMPACT_LOOP_NEW and verdict != "success" and not compact_nudged:
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
            return _leave("success", messages)
        if verdict == "question":
            if nudged:
                return _leave("asking", messages)
            baseline_assistant_id = last_assistant_id(messages)
            _post_user(job, client, store, "UNATTENDED_NUDGE", prompts.UNATTENDED_NUDGE)
            nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        if verdict == "compact_leftover":
            if compact_nudged:
                return _leave("compact_leftover", messages)
            baseline_assistant_id = last_assistant_id(messages)
            _post_user(job, client, store, "COMPACT_LOOP_NUDGE", prompts.COMPACT_LOOP_NUDGE)
            compact_nudged = True
            awaiting_turn = True
            last_progress = time.time()
            continue
        return _leave("incomplete", messages, finish=finish or "(none)")
