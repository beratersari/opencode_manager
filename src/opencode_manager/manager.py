"""Accept, queue, dispatch, boot, shutdown."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from opencode_manager.callback import post_callback
from opencode_manager.cleanup.kill import kill_job_tree, reap_work_dir
from opencode_manager.cleanup.rmtree import hard_delete
from opencode_manager.dashboard.store import JobStore
from opencode_manager.log import get_logger
from opencode_manager.models import (
    Envelope,
    JobRecord,
    JobRequest,
    callback_host_allowed,
    mint_job_id,
    utc_now,
    validate_request_fields,
)
from opencode_manager.queue import JobQueue
from opencode_manager.settings import Settings
from opencode_manager.worker import JobRunner, OpenCodeRunner, Terminal, finish_job, run_pipeline

logger = get_logger()


class Manager:
    def __init__(self, settings: Settings, runner: Optional[JobRunner] = None) -> None:
        self.settings = settings
        self.store = JobStore(settings.job_store_dir)
        self.queue = JobQueue(settings.queue_path)
        self.runner = runner or OpenCodeRunner(settings, self.store)
        self.ready = False
        self.stopping = False
        self._lock = threading.RLock()
        self._running = 0
        self._pats: Dict[str, str] = {}
        self._threads: list[threading.Thread] = []

    def boot(self) -> None:
        logger.info("boot: reaping orphans under %s", self.settings.work_dir)
        reap_work_dir(self.settings.work_dir)
        for job in self.store.list_all():
            if job.status in {"queued", "running"}:
                logger.info("boot leftover %s -> ERROR", job.job_id)
                finish_job(
                    job,
                    Terminal(500, "process restarted; leftover job was not resumed"),
                    settings=self.settings,
                    store=self.store,
                    send_callback=False,
                )
        leftovers = self.queue.clear()
        for row in leftovers:
            job_id = str(row.get("job_id") or "")
            job = self.store.get(job_id) if job_id else None
            if job and job.status in {"queued", "running"}:
                finish_job(
                    job,
                    Terminal(500, "process restarted; leftover queued job was not resumed"),
                    settings=self.settings,
                    store=self.store,
                    send_callback=False,
                )
        self.ready = True
        logger.info("boot finished")

    def shutdown(self) -> None:
        self.stopping = True
        self.ready = False
        logger.info("shutdown: failing live jobs")
        queued = self.queue.clear()
        live = [j for j in self.store.list_all() if j.status in {"queued", "running"}]
        seen = {j.job_id for j in live}
        for row in queued:
            job_id = str(row.get("job_id") or "")
            if job_id and job_id not in seen:
                job = self.store.get(job_id)
                if job:
                    live.append(job)
        for job in live:
            kill_job_tree([job.serve_pid, *job.extra_pids])
            finish_job(
                job,
                Terminal(500, "manager shutting down"),
                settings=self.settings,
                store=self.store,
                send_callback=True,
            )
            if job.clone_path:
                from pathlib import Path

                hard_delete(Path(job.clone_path))
        for thread in list(self._threads):
            thread.join(timeout=2)

    def submit(self, body: Dict[str, Any]) -> tuple[int, Envelope]:
        if not self.ready or self.stopping:
            return 503, Envelope(
                text="manager is not accepting jobs",
                session_id="",
                status_code=503,
                jira_id=str(body.get("jira_id") or ""),
                job_id="",
            )
        err = validate_request_fields(body)
        if err:
            return 400, Envelope(
                text=err,
                session_id="",
                status_code=400,
                jira_id=str(body.get("jira_id") or ""),
                job_id="",
            )
        req = JobRequest.model_validate(
            {**body, "retry_count": max(1, int(body["retry_count"]))}
        )
        if not callback_host_allowed(req.callback_url, self.settings.callback_allowed_hosts):
            return 400, Envelope(
                text="callback_url host is not allowed",
                session_id="",
                status_code=400,
                jira_id=req.jira_id,
                job_id="",
            )
        with self._lock:
            live = self.store.live_for_jira(req.jira_id)
            if live:
                return 409, Envelope(
                    text=f"jira_id {req.jira_id} already has a live job",
                    session_id=live.session_id or "",
                    status_code=409,
                    jira_id=req.jira_id,
                    job_id=live.job_id,
                )
            job_id = mint_job_id()
            job = JobRecord(
                job_id=job_id,
                jira_id=req.jira_id,
                status="queued",
                live=True,
                agent_mode=req.agent_mode,
                model=req.model,
                session_id=req.session_id or "",
                repo_url=_public_repo(req.repo_url),
                source_branch=req.source_branch,
                timeout_in_seconds=req.timeout_in_seconds,
                retry_count=req.retry_count,
                accepted_at=utc_now(),
                callback_url=req.callback_url,
                prompt=req.prompt,
            )
            self.store.save(job)
            payload = req.model_dump()
            payload["job_id"] = job_id
            payload["accepted_at"] = job.accepted_at
            started = False
            if self._running < self.settings.max_concurrent_jobs:
                self._running += 1
                job.status = "running"
                job.started_at = utc_now()
                self.store.save(job)
                started = True
                self._start_thread(job, req.PAT)
                text = "Job accepted and is now in progress."
            else:
                self.queue.enqueue(payload)
                text = "Job accepted and queued."
            code = 202
            return code, Envelope(
                text=text,
                session_id=job.session_id or "",
                status_code=202,
                jira_id=job.jira_id,
                job_id=job.job_id,
            )

    def _start_thread(self, job: JobRecord, pat: str) -> None:
        job._pat = pat  # type: ignore[attr-defined]

        def _target() -> None:
            try:
                run_pipeline(
                    job,
                    settings=self.settings,
                    store=self.store,
                    runner=self.runner,
                    should_stop=lambda: self.stopping,
                    send_callback=True,
                )
            finally:
                self._on_done()

        thread = threading.Thread(target=_target, name=f"osm-{job.job_id}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _on_done(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            if self.stopping:
                return
            if self._running >= self.settings.max_concurrent_jobs:
                return
            nxt = self.queue.dequeue()
            if not nxt:
                return
            job = self.store.get(str(nxt.get("job_id") or ""))
            if not job:
                return
            self._running += 1
            self._start_thread(job, str(nxt.get("PAT") or ""))

    def job_public(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.store.get(job_id)
        if not job:
            return None
        return job.public_dict()


def _public_repo(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()
    return url
