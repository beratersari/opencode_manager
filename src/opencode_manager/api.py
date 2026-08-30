"""HTTP routes: POST /jobs plus read-only dashboard GET /api/*."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from opencode_manager import __version__
from opencode_manager.dashboard.chat import job_chat_payload
from opencode_manager.log import get_logger, read_job_log_lines, redact
from opencode_manager.manager import Manager
from opencode_manager.models import Envelope, LIST_FILTERS, job_matches_list_filter, utc_now

router = APIRouter()


def _mgr(request: Request) -> Manager:
    return request.app.state.manager


@router.post("/jobs")
async def post_jobs(request: Request) -> JSONResponse:
    manager = _mgr(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    get_logger().info(
        "inbound POST /jobs jira_id=%s agent=%s model=%s branch=%s repo=%s session=%s",
        body.get("jira_id"),
        body.get("agent_mode"),
        body.get("model"),
        body.get("source_branch"),
        redact(str(body.get("repo_url") or "")),
        body.get("session_id") or "",
    )
    status, envelope = manager.submit(body)
    get_logger().info(
        "inbound POST /jobs ack HTTP %s job_id=%s status_code=%s",
        status,
        envelope.job_id,
        envelope.status_code,
    )
    return JSONResponse(envelope.model_dump(), status_code=status)


@router.get("/api/meta")
def api_meta() -> Dict[str, Any]:
    return {
        "version": __version__,
        "server_time": utc_now(),
        "app_name": "OpenCode Session Manager",
    }


@router.get("/api/jobs")
def api_jobs(
    request: Request,
    jira_id: Optional[str] = None,
    filter: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Dict[str, Any]:
    jobs = _mgr(request).store.list_all()
    if jira_id:
        key = jira_id.strip()
        jobs = [j for j in jobs if j.jira_id == key]
    filt = (filter or "all").strip().lower()
    if filt not in LIST_FILTERS:
        filt = "all"
    if filt != "all":
        jobs = [j for j in jobs if job_matches_list_filter(j, filt)]
    total = len(jobs)
    start = (page - 1) * page_size
    slice_ = jobs[start : start + page_size]
    return {
        "jobs": [j.public_dict() for j in slice_],
        "total": total,
        "page": page,
        "page_size": page_size,
        "filter": filt,
        "server_time": utc_now(),
    }


@router.get("/api/jobs/{job_id}")
def api_job(job_id: str, request: Request) -> Dict[str, Any]:
    manager = _mgr(request)
    job = manager.store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    logs = read_job_log_lines(
        manager.settings.job_log_dir, job.jira_id, job.job_id, log_file=job.log_file
    )
    return {"job": job.public_dict(), "system_logs": logs, "server_time": utc_now()}


@router.get("/api/jobs/{job_id}/prompts")
def api_prompts(job_id: str, request: Request) -> Dict[str, Any]:
    job = _mgr(request).store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    return {
        "job_id": job.job_id,
        "prompts": [p.model_dump() for p in job.prompts],
        "server_time": utc_now(),
    }


@router.get("/api/jobs/{job_id}/chat")
def api_chat(job_id: str, request: Request) -> Dict[str, Any]:
    job = _mgr(request).store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    payload = job_chat_payload(job)
    payload["server_time"] = utc_now()
    return payload


@router.get("/api/jobs/{job_id}/logs")
def api_logs(job_id: str, request: Request) -> Dict[str, Any]:
    manager = _mgr(request)
    job = manager.store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    lines = read_job_log_lines(
        manager.settings.job_log_dir, job.jira_id, job.job_id, log_file=job.log_file
    )
    return {"job_id": job.job_id, "lines": lines, "server_time": utc_now()}


@router.get("/api/queue")
def api_queue(request: Request, jira_id: Optional[str] = None) -> Dict[str, Any]:
    items = _mgr(request).queue.public_items(jira_id=jira_id)
    return {
        "items": items,
        "queued_count": len(items),
        "server_time": utc_now(),
    }


@router.api_route("/api/{full_path:path}", methods=["POST", "PATCH", "PUT", "DELETE"])
async def api_writes_blocked(full_path: str) -> JSONResponse:
    return JSONResponse({"detail": "dashboard is GET-only"}, status_code=405)


def attach_spa(app: FastAPI, dist: Path) -> None:
    if not dist.is_dir():
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def spa_root() -> FileResponse:
        return FileResponse(dist / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
