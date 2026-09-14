"""HTTP routes: POST /jobs, GET /jobs/:id, DELETE /sessions, plus GET-only /api/*."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from opencode_manager import __version__
from opencode_manager.brand import APP_NAME
from opencode_manager.dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    credentials_ok,
    dashboard_auth_required,
    has_username,
    make_session,
    request_authenticated,
)
from opencode_manager.dashboard.chat import job_chat_payload
from opencode_manager.dashboard.report import build_report_context
from opencode_manager.dashboard.runtime_settings import (
    SettingsError,
    save_runtime_settings,
    suggested_agents,
    suggested_models,
)
from opencode_manager.log import get_logger, read_job_log_lines, redact
from opencode_manager.manager import Manager
from opencode_manager.models import (
    Envelope,
    LIST_FILTERS,
    dashboard_visible,
    job_matches_list_filter,
    posted_prompt_rows,
    utc_now,
)
from opencode_manager.opencode.serve import read_serve_log, serve_log_path

router = APIRouter()


def _mgr(request: Request) -> Manager:
    return request.app.state.manager


def _settings(request: Request):
    return request.app.state.settings


def _cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _require_auth(request: Request) -> None:
    settings = _settings(request)
    if request_authenticated(
        settings,
        cookie=request.cookies.get(SESSION_COOKIE) or "",
        headers=request.headers,
    ):
        return
    raise HTTPException(status_code=401, detail="authentication required")


@router.get("/api/auth")
def api_auth(request: Request) -> dict:
    settings = _settings(request)
    return {
        "required": dashboard_auth_required(settings),
        "authenticated": request_authenticated(
            settings,
            cookie=request.cookies.get(SESSION_COOKIE) or "",
            headers=request.headers,
        ),
        "has_username": has_username(settings),
    }


@router.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    settings = _settings(request)
    if not dashboard_auth_required(settings):
        return JSONResponse({"ok": True, "required": False})
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    if not credentials_ok(settings, username, password):
        raise HTTPException(status_code=401, detail="invalid username or password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        value=make_session(settings),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        max_age=SESSION_MAX_AGE,
        path="/",
    )
    return response


@router.post("/api/logout")
def api_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/jobs")
async def post_jobs(request: Request) -> JSONResponse:
    _require_auth(request)
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
    try:
        status, envelope = manager.submit(body)
    except Exception as exc:  # noqa: BLE001
        get_logger().exception("POST /jobs crashed")
        envelope = Envelope(
            text=f"internal error: {exc}",
            session_id="",
            status_code=500,
            jira_id=str(body.get("jira_id") or ""),
            job_id="",
        )
        return JSONResponse(envelope.model_dump(), status_code=500)
    get_logger().info(
        "inbound POST /jobs ack HTTP %s job_id=%s status_code=%s",
        status,
        envelope.job_id,
        envelope.status_code,
    )
    return JSONResponse(envelope.model_dump(), status_code=status)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> JSONResponse:
    """n8n poller: same envelope as the callback, plus live/status."""
    _require_auth(request)
    status, payload = _mgr(request).poll_job(job_id)
    return JSONResponse(payload, status_code=status)


@router.delete("/sessions")
async def delete_sessions(request: Request) -> JSONResponse:
    _require_auth(request)
    manager = _mgr(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    get_logger().info(
        "inbound DELETE /sessions jira_id=%s session=%s",
        body.get("jira_id"),
        body.get("session_id") or "",
    )
    try:
        status, envelope = manager.delete_session(body)
    except Exception as exc:  # noqa: BLE001
        get_logger().exception("DELETE /sessions crashed")
        envelope = Envelope(
            text=f"internal error: {exc}",
            session_id=str(body.get("session_id") or ""),
            status_code=500,
            jira_id=str(body.get("jira_id") or ""),
            job_id="",
        )
        return JSONResponse(envelope.model_dump(), status_code=500)
    get_logger().info(
        "inbound DELETE /sessions ack HTTP %s status_code=%s",
        status,
        envelope.status_code,
    )
    return JSONResponse(envelope.model_dump(), status_code=status)


@router.get("/api/meta")
def api_meta(request: Request) -> Dict[str, Any]:
    _require_auth(request)
    return {
        "version": __version__,
        "server_time": utc_now(),
        "app_name": APP_NAME,
    }


@router.get("/api/jobs")
def api_jobs(
    request: Request,
    jira_id: Optional[str] = None,
    filter: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Dict[str, Any]:
    _require_auth(request)
    try:
        jobs = _mgr(request).store.list_all()
    except Exception:  # noqa: BLE001
        get_logger().exception("GET /api/jobs list failed")
        jobs = []
    jobs = [j for j in jobs if dashboard_visible(j)]
    if jira_id:
        key = jira_id.strip()
        jobs = [j for j in jobs if j.jira_id == key]
    filt = (filter or "all").strip().lower()
    if filt not in LIST_FILTERS:
        filt = "all"
    jobs = [j for j in jobs if job_matches_list_filter(j, filt)]
    total = len(jobs)
    start = (page - 1) * page_size
    slice_ = jobs[start : start + page_size]
    listed = []
    for job in slice_:
        row = job.public_dict()
        row.pop("chat_snapshot", None)
        row.pop("prompts", None)
        listed.append(row)
    return {
        "jobs": listed,
        "total": total,
        "page": page,
        "page_size": page_size,
        "filter": filt,
        "server_time": utc_now(),
    }


@router.get("/api/jobs/{job_id}")
def api_job(job_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    manager = _mgr(request)
    job = manager.store.get(job_id)
    if not job or not dashboard_visible(job):
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    logs = read_job_log_lines(
        manager.settings.job_log_dir, job.jira_id, job.job_id, log_file=job.log_file
    )
    return {"job": job.public_dict(), "system_logs": logs, "server_time": utc_now()}


@router.get("/api/jobs/{job_id}/prompts")
def api_prompts(job_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    job = _mgr(request).store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    return {
        "job_id": job.job_id,
        "prompts": [p.model_dump() for p in posted_prompt_rows(job)],
        "server_time": utc_now(),
    }


@router.get("/api/jobs/{job_id}/chat")
def api_chat(job_id: str, request: Request) -> Dict[str, Any]:
    _require_auth(request)
    job = _mgr(request).store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    payload = job_chat_payload(job)
    payload["server_time"] = utc_now()
    return payload


@router.get("/api/jobs/{job_id}/logs")
def api_logs(
    job_id: str,
    request: Request,
    limit: int = Query(default=2000, ge=0),
) -> Dict[str, Any]:
    _require_auth(request)
    manager = _mgr(request)
    job = manager.store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    lines = read_job_log_lines(
        manager.settings.job_log_dir,
        job.jira_id,
        job.job_id,
        log_file=job.log_file,
        limit=limit,
    )
    return {"job_id": job.job_id, "lines": lines, "server_time": utc_now()}


@router.get("/api/jobs/{job_id}/serve-log")
def api_serve_log(job_id: str, request: Request) -> Dict[str, Any]:
    """OpenCode serve stdout/stderr for this job. GET-only; used by report zip."""
    _require_auth(request)
    manager = _mgr(request)
    job = manager.store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    path = serve_log_path(manager.settings.serve_dir, job.job_id)
    text = read_serve_log(path)
    return {
        "job_id": job.job_id,
        "missing": not path.is_file(),
        "text": text,
        "server_time": utc_now(),
    }


@router.get("/api/queue")
def api_queue(request: Request, jira_id: Optional[str] = None) -> Dict[str, Any]:
    _require_auth(request)
    items = _mgr(request).queue.public_items(jira_id=jira_id)
    return {
        "items": items,
        "queued_count": len(items),
        "server_time": utc_now(),
    }


@router.get("/api/report-context")
def api_report_context(request: Request) -> Dict[str, Any]:
    """Process extras for the client-built issue zip. GET-only; note is not stored."""
    _require_auth(request)
    return build_report_context(_mgr(request))


def _public_listen_host(host: str) -> str:
    text = (host or "").strip() or "127.0.0.1"
    if text in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return text.strip("[]")


def webhook_info_urls(*, listen_host: str, listen_port: int) -> Dict[str, str]:
    """Copy-paste hook URLs for the Settings page. Not stored."""
    base = f"http://{_public_listen_host(listen_host)}:{int(listen_port)}"
    return {
        "webhook_gitlab_url": f"{base}/amirmini/webhook/gitlab",
        "webhook_azure_url": f"{base}/amirmini/webhook/azure",
    }


def _review_settings_payload(request: Request) -> Dict[str, Any]:
    cfg = request.app.state.config
    extras: list[str] = []
    agent_extras: list[str] = []
    manager = getattr(request.app.state, "manager", None)
    if manager is not None:
        extras = [str(job.model or "") for job in manager.store.list_all()]
        agent_extras = [str(getattr(job, "agent", "") or job.agent_mode or "") for job in manager.store.list_all()]
    settings = getattr(request.app.state, "settings", None)
    host = getattr(settings, "listen_host", "127.0.0.1")
    port = int(getattr(settings, "listen_port", 4096) or 4096)
    return {
        "review_model": cfg.opencode_model,
        "review_timeout_seconds": cfg.opencode_timeout,
        "review_agent": cfg.opencode_agent,
        "env_model": (cfg.opencode_model_env or cfg.opencode_model or "").strip(),
        "env_timeout": int(cfg.opencode_timeout_env or cfg.opencode_timeout or 1800),
        "env_agent": (cfg.opencode_agent_env or cfg.opencode_agent or "").strip(),
        "models": suggested_models(cfg, extras),
        "agents": suggested_agents(cfg, agent_extras),
        **webhook_info_urls(listen_host=str(host or ""), listen_port=port),
    }


@router.get("/api/settings")
def api_settings(request: Request) -> Dict[str, Any]:
    _require_auth(request)
    return _review_settings_payload(request)


@router.put("/api/settings")
async def api_put_settings(request: Request) -> Dict[str, Any]:
    _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = request.app.state.config
    model = body.get("review_model", body.get("opencode_model", cfg.opencode_model))
    timeout = body.get("review_timeout_seconds", body.get("opencode_timeout", cfg.opencode_timeout))
    agent = body.get("review_agent", body.get("opencode_agent", cfg.opencode_agent))
    try:
        save_runtime_settings(cfg, model=model, timeout=timeout, agent=agent)
    except SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _review_settings_payload(request)


@router.api_route("/api/{full_path:path}", methods=["POST", "PATCH", "PUT", "DELETE"])
async def api_writes_blocked(full_path: str) -> JSONResponse:
    return JSONResponse({"detail": "dashboard is GET-only"}, status_code=405)


def spa_file_for(dist: Path, full_path: str) -> Path:
    """Serve only a strict child of ``dist``. Escape → ``index.html``."""
    index = dist / "index.html"
    try:
        root = dist.resolve()
        candidate = (dist / (full_path or "")).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return index
    if candidate.is_file():
        return candidate
    return index


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
        return FileResponse(spa_file_for(dist, full_path))
