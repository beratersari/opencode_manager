"""ASGI app factory and CLI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from opencode_manager.api import attach_spa, router
from opencode_manager.brand import APP_NAME
from opencode_manager.crash import install_crash_logging, mark_clean_shutdown
from opencode_manager.log import get_logger, setup_logging
from opencode_manager.manager import Manager
from opencode_manager.models import utc_now
from opencode_manager.settings import Settings, load_settings
from opencode_manager.worker import JobRunner


def create_app(
    settings: Optional[Settings] = None,
    *,
    runner: Optional[JobRunner] = None,
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.boot()
        yield
        try:
            manager.shutdown()
        except Exception:  # noqa: BLE001
            get_logger().exception("lifespan shutdown failed")
        mark_clean_shutdown()

    app = FastAPI(title=APP_NAME, lifespan=lifespan)
    app.state.manager = manager
    app.state.settings = settings
    app.include_router(router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
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
