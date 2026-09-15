"""ASGI app factory and CLI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from opencode_manager.api import attach_spa, router
from opencode_manager.azure.client import AzureClient
from opencode_manager.brand import APP_NAME
from opencode_manager.crash import install_crash_logging, mark_clean_shutdown
from opencode_manager.gitlab.client import GitLabClient
from opencode_manager.log import get_logger, setup_logging
from opencode_manager.manager import Manager
from opencode_manager.models import utc_now
from opencode_manager.review_config import review_config_from_settings
from opencode_manager.review_manager import ReviewManager
from opencode_manager.review_worker import OpenCodeRunner as ReviewRunner
from opencode_manager.settings import Settings, load_settings
from opencode_manager.webhook_azure import router as azure_webhook_router
from opencode_manager.webhook_gitlab import router as gitlab_webhook_router
from opencode_manager.worker import JobRunner
from opencode_manager.workspace.store import WorkspaceStore


def create_app(
    settings: Optional[Settings] = None,
    *,
    runner: Optional[JobRunner] = None,
    review_runner: Optional[object] = None,
) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    setup_logging(
        job_log_dir=settings.job_log_dir,
        app_log=settings.app_log_path,
        level=settings.log_level,
    )
    try:
        crash_path = install_crash_logging(settings.job_log_dir)
        get_logger().info("crash log %s", crash_path)
    except Exception:  # noqa: BLE001
        get_logger().exception("crash logging disabled")
    manager = Manager(settings, runner=runner)
    review_cfg = review_config_from_settings(settings)
    from opencode_manager.dashboard.runtime_settings import apply_runtime_settings

    apply_runtime_settings(review_cfg)
    gitlab = GitLabClient(review_cfg.gitlab_url, review_cfg.gitlab_token)
    azure = (
        AzureClient(
            review_cfg.azure_url,
            review_cfg.azure_token,
            api_version=review_cfg.azure_api_version,
        )
        if review_cfg.azure_enabled
        else None
    )
    workspaces = WorkspaceStore(review_cfg.data_dir / "workspace_meta")
    review_runner = review_runner or ReviewRunner(
        review_cfg,
        workspaces,
        gitlab=gitlab,
        store=manager.store,
        azure=azure,
    )
    reviews = ReviewManager(
        review_cfg,
        review_runner,
        store=manager.store,
        workspaces=workspaces,
    )
    manager.reviews = reviews

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.boot()
        try:
            reviews.boot()
        except Exception:  # noqa: BLE001
            get_logger().exception("review manager boot failed")
        yield
        try:
            reviews.shutdown()
        except Exception:  # noqa: BLE001
            get_logger().exception("review manager shutdown failed")
        try:
            manager.shutdown()
        except Exception:  # noqa: BLE001
            get_logger().exception("lifespan shutdown failed")
        mark_clean_shutdown()

    app = FastAPI(title=APP_NAME, lifespan=lifespan)
    app.state.manager = manager
    app.state.review_manager = reviews
    app.state.settings = settings
    app.state.config = review_cfg
    app.state.gitlab = gitlab
    app.state.azure = azure
    app.include_router(router)
    app.include_router(gitlab_webhook_router)
    app.include_router(azure_webhook_router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        from opencode_manager.dashboard.auth import SESSION_COOKIE, request_authenticated

        if not request_authenticated(
            settings,
            cookie=ws.cookies.get(SESSION_COOKIE) or "",
            headers=ws.headers,
        ):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                running, queued = manager.live_counts()
                await ws.send_json(
                    {
                        "running": running,
                        "queue_queued": queued,
                        "server_time": utc_now(),
                    }
                )
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
        except WebSocketDisconnect:
            return

    attach_spa(app, settings.project_root / "web" / "dist")
    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
