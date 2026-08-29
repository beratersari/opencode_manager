"""One accepted job: clone → OpenCode loop → callback → delete clone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from opencode_manager.callback import post_callback
from opencode_manager.cleanup.kill import kill_job_tree
from opencode_manager.cleanup.rmtree import hard_delete
from opencode_manager.dashboard.store import JobStore
from opencode_manager.git.clone import GitError, clone_path_for, clone_repo, ls_remote_has_branch
from opencode_manager.log import get_logger
from opencode_manager.log_context import bind, clear
from opencode_manager.models import Envelope, JobRecord, utc_now
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.settings import Settings

logger = get_logger()


@dataclass
class Terminal:
    status_code: int
    text: str


class JobRunner(Protocol):
    def run(self, job: JobRecord, *, should_stop: Callable[[], bool]) -> Terminal: ...


class OpenCodeRunner:
    def __init__(self, settings: Settings, store: JobStore) -> None:
        self.settings = settings
        self.store = store

    def run(self, job: JobRecord, *, should_stop: Callable[[], bool]) -> Terminal:
        dest = clone_path_for(
            self.settings.work_dir, job.jira_id, job.repo_url, job.source_branch
        )
        job.clone_path = str(dest)
        self.store.save(job)
        if dest.exists():
            logger.info("deleting leftover dest %s", dest)
            hard_delete(dest)
        try:
            if not ls_remote_has_branch(
                job.repo_url,
                job.source_branch,
                pat=job._pat,  # type: ignore[attr-defined]
                timeout=self.settings.git_clone_timeout_seconds,
            ):
                return Terminal(404, f"source_branch {job.source_branch!r} does not exist on the remote")
            clone_repo(
                job.repo_url,
                dest,
                job.source_branch,
                pat=job._pat,  # type: ignore[attr-defined]
                timeout=self.settings.git_clone_timeout_seconds,
            )
        except GitError as exc:
            if exc.missing_branch:
                return Terminal(404, str(exc))
            return Terminal(500, f"git failed: {exc}")
        try:
            result = run_opencode_job(
                job,
                settings=self.settings,
                store=self.store,
                clone=dest,
                should_stop=should_stop,
            )
            return Terminal(result.status_code, result.text)
        except JobFailed as exc:
            return Terminal(exc.status_code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker crashed")
            return Terminal(500, f"worker crashed: {exc}")
        finally:
            kill_job_tree([job.serve_pid, *job.extra_pids])
            if dest.exists():
                hard_delete(dest)


def finish_job(
    job: JobRecord,
    terminal: Terminal,
    *,
    settings: Settings,
    store: JobStore,
    send_callback: bool = True,
) -> None:
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
    store.save(job)
    if send_callback and job.callback_url:
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


def run_pipeline(
    job: JobRecord,
    *,
    settings: Settings,
    store: JobStore,
    runner: JobRunner,
    should_stop: Callable[[], bool],
    send_callback: bool = True,
) -> None:
    bind(job.job_id, job.jira_id)
    try:
        job.status = "running"
        job.started_at = job.started_at or utc_now()
        store.save(job)
        terminal = runner.run(job, should_stop=should_stop)
        finish_job(job, terminal, settings=settings, store=store, send_callback=send_callback)
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
