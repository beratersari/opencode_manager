from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Optional

from opencode_manager.review_config import ReviewConfig
from opencode_manager.gitlab.events import CleanupTrigger, ReviewTrigger
from opencode_manager.models import JobRecord, mint_job_id, utc_now
from opencode_manager.review_fifo import JobQueue
from opencode_manager.dashboard.store import JobStore
from opencode_manager.review_worker import JobRunner, RunResult
from opencode_manager.cleanup.end import delete_clone_path, protect_pids, stop_job_holders
from opencode_manager.cleanup.kill import kill_job_tree, reap_work_dir
from opencode_manager.log_context import bound
from opencode_manager.diag import log_diag, merge_job_diag
from opencode_manager.review_log import get_logger, log_fail, log_ok, redact_userinfo
from opencode_manager.workspace.identity import clone_path_for, mr_key
from opencode_manager.workspace.store import WorkspaceStore

logger = get_logger("manager")


class ReviewManager:
    def __init__(
        self,
        config: ReviewConfig,
        runner: JobRunner,
        store: Optional[JobStore] = None,
        queue: Optional[JobQueue] = None,
        workspaces: Optional[WorkspaceStore] = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.store = store or JobStore(config.job_dir)
        self.queue = queue or JobQueue(config.data_dir / "review_queue.json")
        self.workspaces = workspaces or WorkspaceStore(config.data_dir / "workspace_meta")
        if hasattr(self.runner, "store"):
            self.runner.store = self.store
        self.ready = False
        self.stopping = False
        self._lock = threading.RLock()
        self._running = 0
        self._running_mr: set[str] = set()
        self._cancel: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._draining_mr: set[str] = set()

    def boot(self) -> None:
        leftover = [
            j
            for j in self.store.list_all()
            if j.status in {"queued", "running"} and getattr(j, "job_kind", "") == "review"
        ]
        pids: list[Optional[int]] = []
        for job in leftover:
            pids.extend([job.serve_pid, *list(job.extra_pids or [])])
        if pids:
            kill_job_tree(pids)
        try:
            reap_work_dir(self.config.work_dir, protect={os.getpid()})
        except Exception:  # noqa: BLE001
            logger.exception("boot reap_work_dir failed")
        for job in leftover:
            if job.status == "running":
                with bound(job.job_id, job.mr_key, job.log_file):
                    self._finish(
                        job,
                        RunResult(error="process restarted; leftover job was not resumed"),
                        status="error",
                    )
        leftover_queued = [j for j in leftover if j.status == "queued"]
        leftover_queued.sort(key=lambda item: (item.accepted_at or "", item.job_id))
        for job in leftover_queued:
            self.queue.enqueue(job.mr_key, job.job_id)
        # queued leftovers stay in the persisted queue and will dispatch
        self.ready = True
        failed = len([j for j in leftover if j.status == "running"])
        queued = len([j for j in leftover if j.status == "queued"])
        log_ok(logger, "manager boot", leftover_running_failed=failed, leftover_queued=queued)
        self._dispatch()

    def shutdown(self) -> None:
        self.stopping = True
        self.ready = False
        with self._lock:
            events = list(self._cancel.values())
        for event in events:
            event.set()
        live = [
            j
            for j in self.store.list_all()
            if j.status in {"queued", "running"} and getattr(j, "job_kind", "") == "review"
        ]
        guarded = protect_pids()
        for job in live:
            if job.status == "running":
                clone = Path(job.clone_path) if job.clone_path else None
                try:
                    stop_job_holders(job, clone, protect=guarded)
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown stop_job_holders failed job=%s", job.job_id)
            elif job.status == "queued":
                self.queue.remove(job.mr_key, job.job_id)
                self._finish(job, RunResult(error="manager shutting down", cancelled=True), status="cancelled")
        for thread in list(self._threads):
            thread.join(timeout=15)
        leftover = [
            j
            for j in self.store.list_all()
            if j.status == "running" and getattr(j, "job_kind", "") == "review"
        ]
        for job in leftover:
            self._finish(job, RunResult(error="manager shutting down", cancelled=True), status="cancelled")
        log_ok(logger, "manager shutdown")

    def submit(self, trigger: ReviewTrigger) -> tuple[str, JobRecord | None, str]:
        """Return (ack, job, message). ack is accepted|queued|ignored."""
        if not self.ready or self.stopping:
            log_fail(logger, "job submit", reason="manager is not accepting jobs", kind=trigger.kind)
            return "ignored", None, "manager is not accepting jobs"
        key = mr_key(trigger.project_id, trigger.mr_iid)
        with self._lock:
            if key in self._draining_mr:
                log_fail(logger, "job submit", reason="MR is closing", mr=key, kind=trigger.kind)
                return "ignored", None, "MR is closing"
            running = self.store.running_for_mr(key)
            queued_ids = self.queue.queued_ids(key)
            if not trigger.explicit and (running or queued_ids):
                log_ok(logger, "job submit skipped", reason="already busy", mr=key, kind=trigger.kind)
                return "ignored", None, "MR already has a running or queued job"
            source = (getattr(trigger, "source", None) or "").strip() or (
                f"{trigger.azure_project}/{trigger.azure_repo}".strip("/")
                if getattr(trigger, "provider", "") == "azure"
                else (trigger.web_url or key)
            )
            job = JobRecord(
                job_id=mint_job_id(),
                jira_id=key,
                job_kind="review",
                source=source,
                mr_key=key,
                project_id=trigger.project_id,
                mr_iid=trigger.mr_iid,
                trigger=trigger.kind,
                status="queued",
                live=True,
                explicit=trigger.explicit,
                comment_text=trigger.comment_text,
                source_branch=trigger.source_branch,
                target_branch=trigger.target_branch,
                sha=trigger.sha,
                web_url=trigger.web_url,
                repo_url=trigger.web_url or "",
                mr_title=(trigger.title or "").strip(),
                provider=getattr(trigger, "provider", None) or "gitlab",
                azure_project=getattr(trigger, "azure_project", None) or "",
                azure_repo=getattr(trigger, "azure_repo", None) or "",
                azure_collection=getattr(trigger, "azure_collection", None) or "",
                discussion_id=getattr(trigger, "discussion_id", None) or "",
                parent_comment_id=int(getattr(trigger, "parent_comment_id", 0) or 0),
                comment_path=getattr(trigger, "comment_path", None) or "",
                comment_side=getattr(trigger, "comment_side", None) or "",
                comment_start_line=int(getattr(trigger, "comment_start_line", 0) or 0),
                comment_end_line=int(getattr(trigger, "comment_end_line", 0) or 0),
                parent_comment_text=getattr(trigger, "parent_comment_text", None) or "",
                model=self.config.opencode_model,
                agent=self.config.opencode_agent,
                agent_mode=self.config.opencode_agent,
                timeout_in_seconds=self.config.opencode_timeout,
                retry_count=self.config.opencode_retry_count,
                accepted_at=utc_now(),
                log_file=f"{key}-{mint_job_id()}.log",
            )
            # fix log file to use job id
            job.log_file = f"{key}-{job.job_id}.log"
            self.store.save(job)
            self.queue.enqueue(key, job.job_id)
            started = self._try_start_locked(key)
        ack = "accepted" if started else "queued"
        with bound(job.job_id, job.mr_key, job.log_file):
            log_ok(
                logger,
                "job submit",
                ack=ack,
                mr=key,
                job=job.job_id,
                trigger=trigger.kind,
                provider=job.provider or "gitlab",
            )
        return ack, job, f"{ack} {job.job_id}"

    def cleanup_mr(self, trigger: CleanupTrigger) -> None:
        key = mr_key(trigger.project_id, trigger.mr_iid)
        log_ok(logger, "cleanup start", mr=key, action=trigger.action)
        # OSM cascade. Trigger is MR close/merge, not job end.
        cancelled, _ = self.cancel_mr(
            trigger.project_id,
            trigger.mr_iid,
            delete_clone_dir=True,
            delete_reason=f"mr-{trigger.action}",
        )
        log_ok(logger, "cleanup done", mr=key, action=trigger.action, cancelled=cancelled)

    def cancel_job(self, job_id: str) -> tuple[bool, str]:
        to_stop: JobRecord | None = None
        with self._lock:
            job = self.store.get(job_id)
            if not job:
                log_fail(logger, "cancel job", job=job_id, reason="not found")
                return False, "not found"
            if job.status not in {"queued", "running"}:
                log_fail(logger, "cancel job", job=job_id, reason=f"job is {job.status}")
                return False, f"job is {job.status}"
            if job.status == "queued":
                self.queue.remove(job.mr_key, job.job_id)
                with bound(job.job_id, job.mr_key, job.log_file):
                    self._finish(job, RunResult(cancelled=True, error="cancelled"), status="cancelled")
                log_ok(logger, "cancel job", job=job_id, status="queued")
                return True, "cancelled queued job"
            event = self._cancel.get(job.job_id)
            if event:
                event.set()
            to_stop = job
        clone = Path(to_stop.clone_path) if to_stop.clone_path else None
        with bound(to_stop.job_id, to_stop.mr_key, to_stop.log_file):
            try:
                stop_job_holders(to_stop, clone, protect=protect_pids())
            except Exception:  # noqa: BLE001
                logger.exception("cancel stop_job_holders failed job=%s", job_id)
                kill_job_tree([to_stop.serve_pid, *list(to_stop.extra_pids or [])])
        log_ok(logger, "cancel job", job=job_id, status="running")
        return True, "cancel requested"

    def cancel_mr(
        self,
        project_id: int,
        mr_iid: int,
        *,
        delete_clone_dir: bool = False,
        delete_reason: str = "mr-merge",
    ) -> tuple[int, str]:
        key = mr_key(project_id, mr_iid)
        # Drain the FIFO first (under the start lock) so _after_job cannot
        # pop the next comment and start it while we cancel the runner.
        with self._lock:
            if delete_clone_dir:
                self._draining_mr.add(key)
            cancelled = self._cancel_queued_locked(key)
            running = self.store.running_for_mr(key)
        try:
            if running:
                ok, _ = self.cancel_job(running.job_id)
                if ok:
                    cancelled += 1
            if delete_clone_dir:
                if running:
                    for thread in list(self._threads):
                        if thread.name == running.job_id:
                            thread.join(timeout=30)
                            break
                    latest = self.store.get(running.job_id)
                    if latest:
                        try:
                            kill_job_tree([latest.serve_pid, *list(latest.extra_pids or [])])
                        except Exception:  # noqa: BLE001
                            logger.exception("cleanup kill leftover pids failed %s", key)
                with self._lock:
                    cancelled += self._cancel_queued_locked(key)
                record = self.workspaces.get(key)
                path = Path(record.clone_path) if record and record.clone_path else clone_path_for(self.config.work_dir, key)
                holders = running
                if holders is None:
                    holders = JobRecord(
                        job_id="cleanup",
                        jira_id=key,
                        job_kind="review",
                        mr_key=key,
                        project_id=project_id,
                        mr_iid=mr_iid,
                        trigger="review",
                    )
                try:
                    stop_job_holders(holders, path, protect=protect_pids())
                except Exception:  # noqa: BLE001
                    logger.exception("stop_job_holders failed %s", key)
                try:
                    delete_clone_path(path, reason=delete_reason)
                    log_ok(logger, "delete clone", mr=key, reason=delete_reason)
                except Exception as exc:  # noqa: BLE001
                    log_fail(logger, "delete clone", mr=key, reason=delete_reason, err=exc)
                self.workspaces.delete(key)
            log_ok(logger, "cancel MR", mr=key, cancelled=cancelled, delete_clone=delete_clone_dir)
            return cancelled, key
        finally:
            if delete_clone_dir:
                with self._lock:
                    self._draining_mr.discard(key)

    def _cancel_queued_locked(self, key: str) -> int:
        cancelled = 0
        for job_id in self.queue.drain(key):
            job = self.store.get(job_id)
            if job and job.status == "queued":
                self._finish(job, RunResult(cancelled=True, error="cancelled"), status="cancelled")
                cancelled += 1
        return cancelled

    def _try_start_locked(self, mr_key_value: str) -> bool:
        if self.stopping or not self.ready:
            return False
        if mr_key_value in self._draining_mr:
            return False
        if mr_key_value in self._running_mr:
            return False
        if self._running >= self.config.max_concurrent_jobs:
            return False
        while True:
            job_id = self.queue.peek(mr_key_value)
            if not job_id:
                return False
            job = self.store.get(job_id)
            if not job or job.status != "queued":
                # Drop this id only. Never pop whoever is at the front now —
                # a concurrent cancel of this head would otherwise steal the next job.
                self.queue.remove(mr_key_value, job_id)
                continue
            if self.queue.pop_if(mr_key_value, job_id) is None:
                continue
            self._running += 1
            self._running_mr.add(mr_key_value)
            event = threading.Event()
            self._cancel[job.job_id] = event
            job.status = "running"
            job.live = True
            job.started_at = utc_now()
            self.store.save(job)
            thread = threading.Thread(target=self._run_job, args=(job.job_id, event), name=job.job_id, daemon=True)
            self._threads.append(thread)
            thread.start()
            return True

    def _dispatch(self) -> None:
        with self._lock:
            # prefer MRs that are idle
            keys = {item["mr_key"] for item in self.queue.public_items()}
            for key in list(keys):
                if self._running >= self.config.max_concurrent_jobs:
                    break
                self._try_start_locked(key)

    def _run_job(self, job_id: str, event: threading.Event) -> None:
        job = self.store.get(job_id)
        if not job:
            self._after_job(None)
            return
        with bound(job.job_id, job.mr_key, job.log_file):
            log_ok(logger, "pipeline start", job=job.job_id, mr=job.mr_key, trigger=job.trigger, provider=job.provider or "gitlab")
            try:
                result = self.runner.run(job, event.is_set)
            except Exception as exc:  # noqa: BLE001
                logger.exception("runner crashed job=%s", job_id)
                log_fail(logger, "pipeline crash", job=job_id, err=exc)
                result = RunResult(error=f"worker crashed: {exc}")
            job = self.store.get(job_id) or job
            if event.is_set() or result.cancelled:
                status = "cancelled"
            elif result.timeout:
                status = "timeout"
            elif result.error or not result.posted:
                if not result.error:
                    result.error = "overview note was not posted"
                status = "error"
            else:
                status = "success"
            self._finish(job, result, status=status)
        self._after_job(job.mr_key)

    def _after_job(self, mr_key_value: Optional[str]) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            if mr_key_value:
                self._running_mr.discard(mr_key_value)
        if mr_key_value:
            with self._lock:
                self._try_start_locked(mr_key_value)
        self._dispatch()

    def _finish(self, job: JobRecord, result: RunResult, *, status: str) -> None:
        job.status = status  # type: ignore[assignment]
        job.live = False
        job.completed_at = utc_now()
        job.text = result.text or job.text
        job.error_message = redact_userinfo(result.error) or None
        if result.session_id:
            job.session_id = result.session_id
        if result.clone_path:
            job.clone_path = result.clone_path
        if result.merge_base:
            job.merge_base = result.merge_base
        if result.sha:
            job.sha = result.sha
        if result.diff_stat:
            job.diff_stat = result.diff_stat
        if result.changed_paths is not None:
            job.changed_paths = result.changed_paths
        if result.chat_snapshot is not None:
            job.chat_snapshot = result.chat_snapshot
        if result.serve_pid:
            job.serve_pid = result.serve_pid
        if result.serve_port:
            job.serve_port = result.serve_port
        merge_job_diag(
            job,
            stage="end",
            status=status,
            posted=bool(result.posted),
            cancelled=bool(result.cancelled),
            error=job.error_message or "",
            clone_path=job.clone_path or "",
            session_id=job.session_id or "",
        )
        log_diag(
            "job",
            "end",
            job=job.job_id,
            mr=job.mr_key,
            status=status,
            error=job.error_message or "",
            provider=job.provider or "gitlab",
        )
        self.store.save(job)
        self._cancel.pop(job.job_id, None)
        if status == "success":
            log_ok(logger, "job finished", job=job.job_id, status=status, posted=result.posted)
        elif status == "cancelled":
            log_ok(logger, "job finished", job=job.job_id, status=status)
        else:
            log_fail(logger, "job finished", job=job.job_id, status=status, err=job.error_message or "")

    def live_counts(self) -> tuple[int, int]:
        with self._lock:
            running = int(self._running)
        try:
            queued = len(self.queue.queued_ids())
        except Exception:  # noqa: BLE001
            queued = 0
        return running, queued

    def health(self) -> dict:
        jobs = [j for j in self.store.list_all() if getattr(j, "job_kind", "") == "review"]
        return {
            "ready": self.ready,
            "running": sum(1 for j in jobs if j.status == "running"),
            "queued": sum(1 for j in jobs if j.status == "queued"),
            "workspaces": len(self.workspaces.list_all()),
        }
