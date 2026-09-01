"""One accepted job: clone → OpenCode loop → callback → delete clone."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from opencode_manager.callback import post_callback
from opencode_manager.cleanup.end import delete_clone_path, stop_job_holders
from opencode_manager.dashboard.store import JobStore, persist_job
from opencode_manager.git.clone import GitError, clone_path_for, clone_repo, ls_remote_has_branch
from opencode_manager.git.detect import classify_host
from opencode_manager.log import clip, get_logger, log_fail
from opencode_manager.log_context import bind, clear
from opencode_manager.models import Envelope, JobRecord, utc_now
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.settings import Settings

logger = get_logger()
_finish_lock = threading.Lock()
_finished_ids: set[str] = set()


@dataclass
class Terminal:
    status_code: int
    text: str


class JobRunner(Protocol):
    def run(self, job: JobRecord, *, should_stop: Callable[[], bool]) -> Terminal: ...


def _remove_clone(dest: Path, *, reason: str) -> bool:
    """Hard-delete dest. True only when the path is gone."""
    return delete_clone_path(dest, reason=reason)


class OpenCodeRunner:
    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store

    def run(self, job: JobRecord, *, should_stop: Callable[[], bool]) -> Terminal:
        dest = None
        dest = clone_path_for(self.settings.work_dir, job.jira_id)
        job.clone_path = str(dest)
        persist_job(self.store, job)
        kind = classify_host(job.repo_url)
        logger.info(
            "pipeline git start host_kind=%s clone_path=%s branch=%s timeout=%ss",
            kind,
            dest,
            job.source_branch,
            self.settings.git_clone_timeout_seconds,
        )
        try:
            if should_stop():
                return Terminal(500, "manager shutting down")
            if dest.exists():
                logger.info("leftover clone exists; hard-delete before clone clone_path=%s", dest)
                if not _remove_clone(dest, reason="before-clone"):
                    log_fail(logger, "leftover clone could not be deleted", clone_path=dest)
                    return Terminal(500, f"could not remove leftover clone at {dest}")
            logger.info("ls-remote check for source_branch=%s", job.source_branch)
            if not ls_remote_has_branch(
                job.repo_url,
                job.source_branch,
                timeout=self.settings.git_clone_timeout_seconds,
                job=job,
                store=self.store,
                should_stop=should_stop,
            ):
                logger.warning(
                    "source_branch missing on remote: %s repo=%s",
                    job.source_branch,
                    job.repo_url,
                )
                return Terminal(404, f"source_branch {job.source_branch!r} does not exist on the remote")
            if should_stop():
                return Terminal(500, "manager shutting down")
            logger.info("source_branch exists; clone only (agent will checkout %s)", job.source_branch)
            clone_repo(
                job.repo_url,
                dest,
                job.source_branch,
                timeout=self.settings.git_clone_timeout_seconds,
                job=job,
                store=self.store,
                should_stop=should_stop,
            )
            logger.info("clone ready at %s", dest)
            logger.info("OpenCode phase start retry_count=%s timeout=%ss", job.retry_count, job.timeout_in_seconds)
            result = run_opencode_job(
                job,
                settings=self.settings,
                store=self.store,
                clone=dest,
                should_stop=should_stop,
            )
            logger.info("OpenCode phase done status=%s text_len=%s", result.status_code, len(result.text or ""))
            return Terminal(result.status_code, result.text)
        except GitError as exc:
            log_fail(
                logger,
                "git failed",
                missing_branch=exc.missing_branch,
                err=exc,
                clone_path=dest,
                branch=job.source_branch,
                repo=job.repo_url,
            )
            if exc.missing_branch:
                return Terminal(404, str(exc))
            return Terminal(500, f"git failed: {exc}")
        except JobFailed as exc:
            log_fail(
                logger,
                "OpenCode job failed",
                status=exc.status_code,
                err=exc.message,
                session=job.session_id,
                attempt=job.attempt,
                retry_count=job.retry_count,
                text=clip(job.text, 240),
            )
            return Terminal(exc.status_code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker crashed")
            log_fail(logger, "worker crashed", err=exc, clone_path=dest, session=job.session_id)
            return Terminal(500, f"worker crashed: {exc}")
        finally:
            try:
                extra = list(job.extra_pids or [])
            except Exception:  # noqa: BLE001
                extra = []
            logger.info(
                "job-end cleanup: kill tree pids=%s then hard-delete clone_path=%s",
                [job.serve_pid, *extra],
                dest,
            )
            if dest is not None:
                try:
                    stop_job_holders(job, dest)
                except Exception:  # noqa: BLE001
                    logger.exception("job-end stop_job_holders failed clone_path=%s", dest)
                try:
                    _remove_clone(dest, reason="job-end")
                except Exception:  # noqa: BLE001
                    logger.exception("job-end delete clone failed clone_path=%s", dest)


def finish_job(
    job: JobRecord,
    terminal: Terminal,
    *,
    settings: Settings,
    store: JobStore,
    send_callback: bool = True,
) -> None:
    with _finish_lock:
        if job.job_id in _finished_ids:
            logger.info(
                "finish_job skip %s already terminal (second caller)",
                job.job_id,
            )
            return
        _finished_ids.add(job.job_id)
    job.live = False
    if terminal.status_code == 200:
        job.status = "success"
    elif terminal.status_code == 404:
        job.status = "not_found"
    elif terminal.status_code == 504:
        job.status = "timeout"
    else:
        job.status = "error"
    job.text = terminal.text
    job.callback_status_code = terminal.status_code
    job.completed_at = utc_now()
    if terminal.status_code != 200:
        job.error_message = terminal.text
    persist_job(store, job)
    logger.info(
        "job terminal status=%s callback_status=%s session=%s send_callback=%s text_len=%s",
        job.status,
        terminal.status_code,
        job.session_id or "",
        send_callback,
        len(terminal.text or ""),
    )
    if send_callback and job.callback_url:
        try:
            post_callback(
                settings,
                Envelope(
                    text=terminal.text,
                    session_id=job.session_id or "",
                    status_code=terminal.status_code,
                    jira_id=job.jira_id,
                    job_id=job.job_id,
                ),
                job.callback_url,
            )
        except Exception:  # noqa: BLE001
            logger.exception("finish_job callback failed job=%s", job.job_id)


def run_pipeline(
    job: JobRecord,
    *,
    settings: Settings,
    store: JobStore,
    runner: JobRunner,
    should_stop: Callable[[], bool],
    send_callback: bool = True,
) -> None:
    bind(job.job_id, job.jira_id, log_file=job.log_file)
    try:
        job.status = "running"
        job.started_at = job.started_at or utc_now()
        persist_job(store, job)
        logger.info("pipeline start log_file=%s clone_root=%s", job.log_file, settings.work_dir)
        terminal = runner.run(job, should_stop=should_stop)
        logger.info("pipeline runner returned %s", terminal.status_code)
        finish_job(job, terminal, settings=settings, store=store, send_callback=send_callback)
        logger.info("pipeline end")
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline failed")
        finish_job(
            job,
            Terminal(500, f"pipeline failed: {exc}"),
            settings=settings,
            store=store,
            send_callback=send_callback,
        )
    finally:
        clear()
