"""Accept, queue, dispatch, boot, shutdown."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from opencode_manager.callback import post_callback
from opencode_manager.cleanup.end import delete_clone_path, protect_pids, stop_job_holders
from opencode_manager.cleanup.kill import kill_job_tree, reap_work_dir
from opencode_manager.dashboard.store import JobStore, persist_job
from opencode_manager.git.clone import GitError, clone_path_for
from opencode_manager.log import get_logger, job_log_filename
from opencode_manager.log_context import bind, clear
from opencode_manager.models import (
    Envelope,
    JobRecord,
    JobRequest,
    callback_host_allowed,
    mint_job_id,
    poll_payload,
    utc_now,
    agent_mode_from_body,
    validate_request_fields,
    validate_session_delete_fields,
)
from opencode_manager.opencode.serve import serve_log_path, start_serve, stop_serve
from opencode_manager.opencode.session import OpenCodeClient
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

        self._threads: list[threading.Thread] = []
        self._session_deletes: set[str] = set()
        self.session_delete_health_timeout = 30.0
        self._start_delete_serve = start_serve
        self._stop_delete_serve = stop_serve
        self._open_code_client_cls = OpenCodeClient

    def boot(self) -> None:
        logger.info(
            "boot start data_dir=%s work_dir=%s job_log_dir=%s job_store_dir=%s max_concurrent=%s",
            self.settings.data_dir,
            self.settings.work_dir,
            self.settings.job_log_dir,
            self.settings.job_store_dir,
            self.settings.max_concurrent_jobs,
        )
        try:
            leftover_pids: list[Optional[int]] = []
            try:
                leftover_jobs = [j for j in self.store.list_all() if j.status in {"queued", "running"}]
            except Exception:  # noqa: BLE001
                logger.exception("boot list leftover jobs failed")
                leftover_jobs = []
            for job in leftover_jobs:
                leftover_pids.extend([job.serve_pid, *list(job.extra_pids or [])])
            if leftover_pids:
                logger.info("boot kill recorded leftover pids=%s", leftover_pids)
                try:
                    kill_job_tree(leftover_pids)
                except Exception:  # noqa: BLE001
                    logger.exception("boot kill leftover pids failed")
            try:
                killed = reap_work_dir(self.settings.work_dir, protect={os.getpid()})
            except Exception:  # noqa: BLE001
                logger.exception("boot reap_work_dir failed")
                killed = 0
            logger.info("boot reap orphans under %s killed=%s", self.settings.work_dir, killed)
            for job in leftover_jobs:
                bind(job.job_id, job.jira_id, log_file=job.log_file)
                logger.info("boot leftover %s status=%s -> ERROR (no callback)", job.job_id, job.status)
                try:
                    finish_job(
                        job,
                        Terminal(500, "process restarted; leftover job was not resumed"),
                        settings=self.settings,
                        store=self.store,
                        send_callback=False,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("boot leftover finish failed job=%s", job.job_id)
                clear()
            try:
                leftovers = self.queue.clear()
            except Exception:  # noqa: BLE001
                logger.exception("boot queue clear failed")
                leftovers = []
            logger.info("boot dropped %s leftover queue rows", len(leftovers))
            for row in leftovers:
                job_id = str(row.get("job_id") or "")
                try:
                    job = self.store.get(job_id) if job_id else None
                except Exception:  # noqa: BLE001
                    logger.exception("boot leftover queue get failed job=%s", job_id)
                    continue
                if job and job.status in {"queued", "running"}:
                    try:
                        finish_job(
                            job,
                            Terminal(500, "process restarted; leftover queued job was not resumed"),
                            settings=self.settings,
                            store=self.store,
                            send_callback=False,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("boot leftover queue finish failed job=%s", job_id)
        except Exception:  # noqa: BLE001
            logger.exception("boot failed; still accepting new jobs")
        finally:
            self.ready = True
            logger.info("boot finished")

    def shutdown(self) -> None:
        self.stopping = True
        self.ready = False
        logger.info("shutdown: stop accepting jobs; failing live work")
        try:
            try:
                queued = self.queue.clear()
            except Exception:  # noqa: BLE001
                logger.exception("shutdown queue clear failed")
                queued = []
            try:
                live = [j for j in self.store.list_all() if j.status in {"queued", "running"}]
            except Exception:  # noqa: BLE001
                logger.exception("shutdown list live jobs failed")
                live = []
            seen = {j.job_id for j in live}
            for row in queued:
                job_id = str(row.get("job_id") or "")
                if job_id and job_id not in seen:
                    try:
                        job = self.store.get(job_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("shutdown get queued job failed job=%s", job_id)
                        continue
                    if job:
                        live.append(job)
            logger.info("shutdown live jobs=%s", [j.job_id for j in live])
            protect = protect_pids()
            for job in live:
                bind(job.job_id, job.jira_id, log_file=job.log_file)
                logger.info(
                    "shutdown fail job serve_pid=%s extra_pids=%s clone=%s",
                    job.serve_pid,
                    job.extra_pids,
                    job.clone_path,
                )
                clone = Path(job.clone_path) if job.clone_path else None
                try:
                    stop_job_holders(job, clone, protect=protect)
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown stop_job_holders failed job=%s", job.job_id)
                try:
                    finish_job(
                        job,
                        Terminal(500, "manager shutting down"),
                        settings=self.settings,
                        store=self.store,
                        send_callback=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown finish_job failed job=%s", job.job_id)
                try:
                    delete_clone_path(clone, reason="shutdown")
                except Exception:  # noqa: BLE001
                    logger.exception("shutdown delete clone failed job=%s", job.job_id)
                clear()
            deadline = time.monotonic() + max(60.0, float(self.settings.git_clone_timeout_seconds))
            for thread in list(self._threads):
                remaining = max(0.1, deadline - time.monotonic())
                thread.join(timeout=remaining)
                if thread.is_alive():
                    logger.error("shutdown: worker thread still alive name=%s", thread.name)
        except Exception:  # noqa: BLE001
            logger.exception("shutdown failed")
        logger.info("shutdown complete")

    def submit(self, body: Dict[str, Any]) -> tuple[int, Envelope]:
        if not self.ready or self.stopping:
            logger.warning("reject POST /jobs: manager not accepting (ready=%s stopping=%s)", self.ready, self.stopping)
            return 503, Envelope(
                text="manager is not accepting jobs",
                session_id="",
                status_code=503,
                jira_id=str(body.get("jira_id") or ""),
                job_id="",
            )
        err = validate_request_fields(body)
        if err:
            keys = sorted(str(k) for k in body.keys() if str(k).upper() != "PAT")
            logger.warning(
                "reject POST /jobs 400: %s keys=%s jira_id=%s model=%s agent=%s branch=%s",
                err,
                keys,
                body.get("jira_id"),
                body.get("model"),
                body.get("agent_mode"),
                body.get("source_branch"),
            )
            return 400, Envelope(
                text=err,
                session_id="",
                status_code=400,
                jira_id=str(body.get("jira_id") or ""),
                job_id="",
            )
        inbound = {**body, "retry_count": max(1, int(body["retry_count"]))}
        if not str(inbound.get("agent_mode") or "").strip():
            inbound["agent_mode"] = agent_mode_from_body(body)
        req = JobRequest.model_validate(inbound)
        if req.callback_url and not callback_host_allowed(
            req.callback_url, self.settings.callback_allowed_hosts
        ):
            logger.warning("reject POST /jobs 400: callback host not allowed")
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
                logger.info(
                    "reject POST /jobs 409: jira_id=%s already live as %s status=%s",
                    req.jira_id,
                    live.job_id,
                    live.status,
                )
                return 409, Envelope(
                    text=f"jira_id {req.jira_id} already has a live job",
                    session_id=live.session_id or "",
                    status_code=409,
                    jira_id=req.jira_id,
                    job_id=live.job_id,
                )
            if req.jira_id in self._session_deletes:
                logger.info(
                    "reject POST /jobs 409: jira_id=%s has a session delete in progress",
                    req.jira_id,
                )
                return 409, Envelope(
                    text=f"jira_id {req.jira_id} has a session delete in progress",
                    session_id="",
                    status_code=409,
                    jira_id=req.jira_id,
                    job_id="",
                )
            job_id = mint_job_id()
            accepted = utc_now()
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
                accepted_at=accepted,
                callback_url=req.callback_url,
                prompt=req.prompt,
                log_file=job_log_filename(req.jira_id, job_id, accepted),
            )
            self.store.save(job)
            bind(job.job_id, job.jira_id, log_file=job.log_file)
            logger.info(
                "trigger accepted log_file=%s repo=%s branch=%s model=%s agent=%s "
                "timeout=%ss retry_count=%s inbound_session=%s callback=%s "
                "running=%s/%s",
                job.log_file,
                job.repo_url,
                job.source_branch,
                job.model,
                job.agent_mode,
                job.timeout_in_seconds,
                job.retry_count,
                job.session_id or "(none)",
                job.callback_url,
                self._running,
                self.settings.max_concurrent_jobs,
            )
            payload = req.model_dump()
            payload["job_id"] = job_id
            payload["accepted_at"] = job.accepted_at
            if self._running < self.settings.max_concurrent_jobs:
                self._running += 1
                job.status = "running"
                job.started_at = utc_now()
                persist_job(self.store, job)
                logger.info("dispatch now (slot free) started_at=%s", job.started_at)
                self._start_thread(job)
                text = "Job accepted and is now in progress."
            else:
                payload.pop("PAT", None)
                try:
                    self.queue.enqueue(payload)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "queue persist failed job=%s jira_id=%s",
                        job.job_id,
                        job.jira_id,
                    )
                    try:
                        finish_job(
                            job,
                            Terminal(500, f"could not persist queue: {exc}"),
                            settings=self.settings,
                            store=self.store,
                            send_callback=False,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "queue persist finish_job failed job=%s", job.job_id
                        )
                        job.live = False
                        job.status = "error"
                        persist_job(self.store, job)
                    clear()
                    return 503, Envelope(
                        text="could not persist queue",
                        session_id=job.session_id or "",
                        status_code=503,
                        jira_id=job.jira_id,
                        job_id="",
                    )
                logger.info("queued FIFO (capacity full)")
                text = "Job accepted and queued."
            clear()
            code = 202
            return code, Envelope(
                text=text,
                session_id=job.session_id or "",
                status_code=202,
                jira_id=job.jira_id,
                job_id=job.job_id,
            )

    def _start_thread(self, job: JobRecord) -> None:
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
            except Exception:  # noqa: BLE001
                logger.exception("worker thread died job=%s", job.job_id)
            finally:
                try:
                    self._on_done()
                except Exception:  # noqa: BLE001
                    logger.exception("_on_done failed job=%s", job.job_id)

        thread = threading.Thread(target=_target, name=f"osm-{job.job_id}", daemon=False)
        self._threads.append(thread)
        thread.start()

    def _on_done(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            if self.stopping:
                return
            if self._running >= self.settings.max_concurrent_jobs:
                return
            while True:
                nxt = self.queue.dequeue()
                if not nxt:
                    logger.info("slot free; queue empty running=%s", self._running)
                    return
                job = self.store.get(str(nxt.get("job_id") or ""))
                if not job:
                    logger.warning("dequeued missing job record %s; trying next", nxt.get("job_id"))
                    continue
                self._running += 1
                logger.info("dequeue %s jira_id=%s running=%s", job.job_id, job.jira_id, self._running)
                self._start_thread(job)
                return

    def live_counts(self) -> tuple[int, int]:
        """Dashboard /ws ticks. Do not parse every job JSON here."""
        with self._lock:
            running = int(self._running)
        try:
            queued = len(self.queue.peek_all())
        except Exception:  # noqa: BLE001
            logger.exception("live_counts queue peek failed")
            queued = 0
        return running, queued

    def job_public(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.store.get(job_id)
        if not job:
            return None
        return job.public_dict()

    def poll_job(self, job_id: str) -> tuple[int, Dict[str, Any]]:
        job = self.store.get(job_id)
        if not job:
            return 404, {
                "text": f"No job {job_id}",
                "session_id": "",
                "status_code": 404,
                "jira_id": "",
                "job_id": job_id,
                "live": False,
                "status": "not_found",
            }
        return poll_payload(job)

    def delete_session(self, body: Dict[str, Any]) -> tuple[int, Envelope]:
        """Sync OpenCode session delete. Not a job. No callback. No history row."""
        raw_jira = str(body.get("jira_id") or "")
        raw_session = str(body.get("session_id") or "")
        if not self.ready or self.stopping:
            logger.warning(
                "reject DELETE /sessions: manager not accepting (ready=%s stopping=%s)",
                self.ready,
                self.stopping,
            )
            return 503, Envelope(
                text="manager is not accepting jobs",
                session_id=raw_session,
                status_code=503,
                jira_id=raw_jira,
                job_id="",
            )
        err = validate_session_delete_fields(body)
        if err:
            logger.warning(
                "reject DELETE /sessions 400: %s jira_id=%s session=%s",
                err,
                body.get("jira_id"),
                body.get("session_id") or "",
            )
            return 400, Envelope(
                text=err,
                session_id=raw_session.strip(),
                status_code=400,
                jira_id=raw_jira.strip(),
                job_id="",
            )
        jira_id = str(body["jira_id"]).strip()
        session_id = str(body["session_id"]).strip()
        with self._lock:
            live = self.store.live_for_jira(jira_id)
            if live:
                logger.info(
                    "reject DELETE /sessions 409: jira_id=%s already live as %s status=%s",
                    jira_id,
                    live.job_id,
                    live.status,
                )
                return 409, Envelope(
                    text=f"jira_id {jira_id} already has a live job",
                    session_id=live.session_id or session_id,
                    status_code=409,
                    jira_id=jira_id,
                    job_id=live.job_id,
                )
            if jira_id in self._session_deletes:
                logger.info(
                    "reject DELETE /sessions 409: jira_id=%s already deleting",
                    jira_id,
                )
                return 409, Envelope(
                    text=f"jira_id {jira_id} has a session delete in progress",
                    session_id=session_id,
                    status_code=409,
                    jira_id=jira_id,
                    job_id="",
                )
            self._session_deletes.add(jira_id)
        bind(jira_id=jira_id)
        handle = None
        client = None
        dest: Optional[Path] = None
        created_dest = False
        try:
            try:
                dest = clone_path_for(self.settings.work_dir, jira_id)
            except GitError as exc:
                logger.warning("reject DELETE /sessions 400: %s", exc)
                return 400, Envelope(
                    text=str(exc),
                    session_id=session_id,
                    status_code=400,
                    jira_id=jira_id,
                    job_id="",
                )
            created_dest = not dest.exists()
            if created_dest:
                dest.mkdir(parents=True, exist_ok=True)
            logger.info(
                "DELETE /sessions start jira_id=%s session=%s dest=%s created_dest=%s",
                jira_id,
                session_id,
                dest,
                created_dest,
            )
            try:
                handle = self._start_delete_serve(
                    bin_name=self.settings.opencode_bin,
                    cwd=dest,
                    log_path=serve_log_path(self.settings.serve_dir, f"session-delete-{jira_id}"),
                    timeout=float(self.session_delete_health_timeout),
                    should_stop=lambda: self.stopping,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("DELETE /sessions serve boot failed jira_id=%s err=%s", jira_id, exc)
                return 500, Envelope(
                    text=f"serve boot failed: {exc}",
                    session_id=session_id,
                    status_code=500,
                    jira_id=jira_id,
                    job_id="",
                )
            client = self._open_code_client_cls(handle.base_url, str(dest))
            try:
                response = client.delete_session(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("DELETE /sessions OpenCode call failed jira_id=%s err=%s", jira_id, exc)
                return 500, Envelope(
                    text=f"session delete failed: {exc}",
                    session_id=session_id,
                    status_code=500,
                    jira_id=jira_id,
                    job_id="",
                )
            if response.status_code >= 400 and response.status_code != 404:
                text = f"OpenCode refused session delete (HTTP {response.status_code})"
                logger.error("DELETE /sessions OpenCode HTTP %s jira_id=%s", response.status_code, jira_id)
                return 500, Envelope(
                    text=text,
                    session_id=session_id,
                    status_code=500,
                    jira_id=jira_id,
                    job_id="",
                )
            logger.info("DELETE /sessions ok jira_id=%s session=%s", jira_id, session_id)
            return 200, Envelope(
                text="session deleted",
                session_id=session_id,
                status_code=200,
                jira_id=jira_id,
                job_id="",
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            if handle is not None:
                try:
                    self._stop_delete_serve(handle)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DELETE /sessions stop serve failed jira_id=%s err=%s", jira_id, exc)
            if created_dest and dest is not None:
                try:
                    delete_clone_path(dest, reason="session-delete-temp")
                except Exception:  # noqa: BLE001
                    logger.exception("DELETE /sessions temp dest cleanup failed jira_id=%s", jira_id)
            with self._lock:
                self._session_deletes.discard(jira_id)
            clear()


def _public_repo(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()
    return url
