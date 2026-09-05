"""One-process launcher: manager API + SPA on :4096 and the :5173 proxy.

Used by the single-file exe (`packaging/build_exe.py`). Does not call
start.bat / start.sh. Git and OpenCode stay on PATH (not bundled).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import uvicorn

from opencode_manager.app import create_app
from opencode_manager.dashboard.frontend_proxy import build_app as build_frontend
from opencode_manager.settings import Settings, executable_dir, load_settings, resource_root

FRONTEND_HOST_DEFAULT = "0.0.0.0"
FRONTEND_PORT_DEFAULT = 5173


@dataclass
class Prepared:
    settings: Settings
    backend_app: object
    frontend_app: object
    frontend_host: str
    frontend_port: int
    dist: Path


def prepend_opencode_path() -> None:
    """Same extra bin dir start-backend prepends; the exe does not vendor OpenCode."""
    extra = Path.home() / ".opencode" / "bin"
    if not extra.is_dir():
        return
    current = os.environ.get("PATH", "")
    prefix = str(extra)
    parts = current.split(os.pathsep) if current else []
    if prefix in parts:
        return
    os.environ["PATH"] = os.pathsep.join([prefix, current] if current else [prefix])


def fallback_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "osm"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "osm"
    return Path.home() / ".local" / "share" / "osm"


def apply_writable_data_dir(settings: Settings) -> Settings:
    """If the default data_dir cannot be created, use a per-user path.

    Zip install.sh writes settings.local.yaml for this. The exe has no installer.
    """
    try:
        settings.ensure_dirs()
        return settings
    except PermissionError:
        fallback = fallback_data_dir()
        print(
            f"[INFO] data_dir {settings.data_dir} is not writable; using {fallback}",
            file=sys.stderr,
        )
        settings.data_dir = fallback
        settings.work_dir = fallback / ".temp"
        settings.job_log_dir = fallback / "logs"
        settings.job_store_dir = fallback / "jobs"
        settings.queue_path = fallback / "queue.json"
        settings.serve_dir = fallback / ".serve"
        settings.app_log_path = fallback / "logs" / "app.log"
        settings.ensure_dirs()
        return settings


def spa_dist(settings: Settings) -> Path:
    return Path(settings.project_root) / "web" / "dist"


def prepare(
    settings: Optional[Settings] = None,
    *,
    frontend_host: str = FRONTEND_HOST_DEFAULT,
    frontend_port: int = FRONTEND_PORT_DEFAULT,
) -> Prepared:
    caller_settings = settings
    settings = settings or load_settings()
    if caller_settings is None or getattr(sys, "frozen", False):
        settings.project_root = resource_root()
    apply_writable_data_dir(settings)
    dist = spa_dist(settings)
    if not (dist / "index.html").is_file():
        raise FileNotFoundError(
            f"SPA missing: {dist / 'index.html'}. "
            "Rebuild the exe after web/dist exists (packaging/build_exe.py)."
        )
    backend_app = create_app(settings)
    backend_url = f"http://127.0.0.1:{settings.listen_port}"
    frontend_app = build_frontend(dist=dist, backend=backend_url)
    return Prepared(
        settings=settings,
        backend_app=backend_app,
        frontend_app=frontend_app,
        frontend_host=frontend_host,
        frontend_port=frontend_port,
        dist=dist,
    )


def _install_stop_signals(backend: uvicorn.Server, frontend: uvicorn.Server) -> None:
    def _stop(*_args: object) -> None:
        backend.should_exit = True
        frontend.should_exit = True

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for sig in (signal.SIGINT, signal.SIGTERM):
        if loop is not None:
            try:
                loop.add_signal_handler(sig, _stop)
                continue
            except (NotImplementedError, RuntimeError):
                pass
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


async def serve_prepared(prepared: Prepared) -> int:
    settings = prepared.settings
    log_level = settings.log_level.lower()
    backend = uvicorn.Server(
        uvicorn.Config(
            prepared.backend_app,
            host=settings.listen_host,
            port=settings.listen_port,
            log_level=log_level,
        )
    )
    frontend = uvicorn.Server(
        uvicorn.Config(
            prepared.frontend_app,
            host=prepared.frontend_host,
            port=prepared.frontend_port,
            log_level=log_level,
        )
    )
    backend.install_signal_handlers = False
    frontend.install_signal_handlers = False
    _install_stop_signals(backend, frontend)

    async def run_frontend() -> None:
        try:
            await frontend.serve()
        except OSError as exc:
            print(
                f"[ERROR] Frontend failed to bind {prepared.frontend_host}:{prepared.frontend_port} ({exc}). "
                f"Backend UI is still at http://127.0.0.1:{settings.listen_port}/jobs",
                file=sys.stderr,
            )

    await asyncio.gather(backend.serve(), run_frontend())
    return 0


def print_banner(prepared: Prepared) -> None:
    port = prepared.settings.listen_port
    print("=" * 50)
    print("  OpenCode Session Manager")
    print("=" * 50)
    print(f"  Backend  : http://127.0.0.1:{port}/        (API + built SPA)")
    print(f"  Dashboard: http://127.0.0.1:{port}/jobs")
    print(
        f"  Frontend : http://127.0.0.1:{prepared.frontend_port}/     (SPA proxy)"
    )
    print(f"  SPA      : {prepared.dist}")
    print(f"  data_dir : {prepared.settings.data_dir}")
    print(f"  overlay  : {executable_dir() / 'settings.local.yaml'}")
    print("  Needs git and opencode on PATH (not inside this exe).")
    print("=" * 50)


def main(argv: Optional[list[str]] = None) -> int:
    from multiprocessing import freeze_support

    freeze_support()
    prepend_opencode_path()
    parser = argparse.ArgumentParser(
        description="OSM single-file launcher (backend + frontend). "
        "Does not use start.bat / start.sh."
    )
    parser.add_argument(
        "--frontend-host",
        default=(os.environ.get("OSM_FRONTEND_HOST") or FRONTEND_HOST_DEFAULT).strip(),
        help=f"SPA proxy bind host (default {FRONTEND_HOST_DEFAULT})",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=int(os.environ.get("OSM_FRONTEND_PORT") or FRONTEND_PORT_DEFAULT),
        help=f"SPA proxy port (default {FRONTEND_PORT_DEFAULT})",
    )
    args = parser.parse_args(argv)
    try:
        prepared = prepare(
            frontend_host=args.frontend_host,
            frontend_port=args.frontend_port,
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print_banner(prepared)
    return asyncio.run(serve_prepared(prepared))


if __name__ == "__main__":
    raise SystemExit(main())
