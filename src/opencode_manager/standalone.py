"""Exe launcher: backend window (:4096) plus a second frontend window (:5173).

Used by the single-file exe (`packaging/build_exe.py`). Does not call
start.bat / start.sh. Git and OpenCode stay on PATH (not bundled).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
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
BACKEND_TITLE = "OSM-Backend"
FRONTEND_TITLE = "OSM-Frontend"


@dataclass
class Prepared:
    settings: Settings
    backend_app: object
    frontend_app: Optional[object]
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
    """Linux-only per-user path. Windows stays on C:\\osm."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "osm"
    return Path.home() / ".local" / "share" / "osm"


def apply_writable_data_dir(settings: Settings) -> Settings:
    """Linux: if /var/lib/osm is not writable, use XDG. Windows: keep C:\\osm."""
    try:
        settings.ensure_dirs()
        return settings
    except PermissionError:
        if os.name == "nt":
            raise
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


def _require_dist(settings: Settings) -> Path:
    dist = spa_dist(settings)
    if not (dist / "index.html").is_file():
        raise FileNotFoundError(
            f"SPA missing: {dist / 'index.html'}. "
            "Rebuild the exe after web/dist exists (packaging/build_exe.py)."
        )
    return dist


def _bind_settings(settings: Optional[Settings]) -> Settings:
    caller = settings
    settings = settings or load_settings()
    if caller is None or getattr(sys, "frozen", False):
        settings.project_root = resource_root()
    return settings


def prepare(
    settings: Optional[Settings] = None,
    *,
    frontend_host: str = FRONTEND_HOST_DEFAULT,
    frontend_port: int = FRONTEND_PORT_DEFAULT,
) -> Prepared:
    settings = _bind_settings(settings)
    apply_writable_data_dir(settings)
    dist = _require_dist(settings)
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


def prepare_backend(
    settings: Optional[Settings] = None,
    *,
    frontend_host: str = FRONTEND_HOST_DEFAULT,
    frontend_port: int = FRONTEND_PORT_DEFAULT,
) -> Prepared:
    settings = _bind_settings(settings)
    apply_writable_data_dir(settings)
    dist = _require_dist(settings)
    return Prepared(
        settings=settings,
        backend_app=create_app(settings),
        frontend_app=None,
        frontend_host=frontend_host,
        frontend_port=frontend_port,
        dist=dist,
    )


def prepare_frontend(
    settings: Optional[Settings] = None,
    *,
    frontend_host: str = FRONTEND_HOST_DEFAULT,
    frontend_port: int = FRONTEND_PORT_DEFAULT,
) -> Prepared:
    settings = _bind_settings(settings)
    dist = _require_dist(settings)
    backend_url = f"http://127.0.0.1:{settings.listen_port}"
    return Prepared(
        settings=settings,
        backend_app=None,
        frontend_app=build_frontend(dist=dist, backend=backend_url),
        frontend_host=frontend_host,
        frontend_port=frontend_port,
        dist=dist,
    )


def set_console_title(title: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        return


def self_command(*extra: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *extra]
    return [sys.executable, "-m", "opencode_manager.standalone", *extra]


def spawn_frontend_window(*, frontend_host: str, frontend_port: int) -> subprocess.Popen:
    """Open a second console running only the SPA proxy."""
    args = self_command(
        "--frontend-only",
        "--frontend-host",
        frontend_host,
        "--frontend-port",
        str(frontend_port),
    )
    cwd = str(executable_dir())
    if os.name == "nt":
        return subprocess.Popen(
            args,
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    for prefix in (
        ["x-terminal-emulator", "-e"],
        ["gnome-terminal", "--"],
        ["konsole", "-e"],
        ["xfce4-terminal", "-e"],
        ["xterm", "-T", FRONTEND_TITLE, "-e"],
    ):
        if shutil.which(prefix[0]):
            return subprocess.Popen([*prefix, *args], cwd=cwd, close_fds=True)
    return subprocess.Popen(args, cwd=cwd, start_new_session=True, close_fds=True)


def _install_stop_signals(*servers: uvicorn.Server) -> None:
    def _stop(*_args: object) -> None:
        for server in servers:
            server.should_exit = True

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


def _uvicorn_server(app: object, host: str, port: int, log_level: str) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=log_level)
    )
    server.install_signal_handlers = False
    return server


async def serve_backend(prepared: Prepared, *, spawn_frontend: bool = False) -> int:
    settings = prepared.settings
    backend = _uvicorn_server(
        prepared.backend_app,
        settings.listen_host,
        settings.listen_port,
        settings.log_level.lower(),
    )
    _install_stop_signals(backend)

    async def maybe_spawn() -> None:
        for _ in range(300):
            if backend.started:
                try:
                    spawn_frontend_window(
                        frontend_host=prepared.frontend_host,
                        frontend_port=prepared.frontend_port,
                    )
                except OSError as exc:
                    print(f"[ERROR] Could not open frontend window ({exc})", file=sys.stderr)
                    return
                print(f"[OK] Opened {FRONTEND_TITLE} window on :{prepared.frontend_port}")
                return
            if backend.should_exit:
                return
            await asyncio.sleep(0.1)
        print("[ERROR] Backend did not start; frontend window not opened", file=sys.stderr)

    tasks = [asyncio.create_task(backend.serve())]
    if spawn_frontend:
        tasks.append(asyncio.create_task(maybe_spawn()))
    await asyncio.gather(*tasks)
    return 0


async def serve_frontend(prepared: Prepared) -> int:
    server = _uvicorn_server(
        prepared.frontend_app,
        prepared.frontend_host,
        prepared.frontend_port,
        prepared.settings.log_level.lower(),
    )
    _install_stop_signals(server)
    try:
        await server.serve()
    except OSError as exc:
        print(
            f"[ERROR] Frontend failed to bind {prepared.frontend_host}:{prepared.frontend_port} ({exc}). "
            f"Backend UI is still at http://127.0.0.1:{prepared.settings.listen_port}/jobs",
            file=sys.stderr,
        )
        return 1
    return 0


async def serve_prepared(prepared: Prepared) -> int:
    """In-process both servers (tests). The exe uses two windows instead."""
    return await serve_backend(prepared, spawn_frontend=False)


def print_backend_banner(prepared: Prepared, *, frontend_window: bool) -> None:
    port = prepared.settings.listen_port
    print("=" * 50)
    print(f"  {BACKEND_TITLE}")
    print("=" * 50)
    print(f"  Backend  : http://127.0.0.1:{port}/        (API + built SPA)")
    print(f"  Dashboard: http://127.0.0.1:{port}/jobs")
    if frontend_window:
        print(f"  Frontend : http://127.0.0.1:{prepared.frontend_port}/     (second window)")
    print(f"  SPA      : {prepared.dist}")
    print(f"  data_dir : {prepared.settings.data_dir}")
    print(f"  overlay  : {executable_dir() / 'settings.local.yaml'}")
    print("  Needs git and opencode on PATH (not inside this exe).")
    print("=" * 50)


def print_frontend_banner(prepared: Prepared) -> None:
    print("=" * 50)
    print(f"  {FRONTEND_TITLE}")
    print("=" * 50)
    print(f"  UI       : http://127.0.0.1:{prepared.frontend_port}/")
    print(f"  Proxies  : /api and /ws  ->  http://127.0.0.1:{prepared.settings.listen_port}")
    print(f"  SPA      : {prepared.dist}")
    print("=" * 50)


def print_banner(prepared: Prepared) -> None:
    print_backend_banner(prepared, frontend_window=True)


def main(argv: Optional[list[str]] = None) -> int:
    from multiprocessing import freeze_support

    freeze_support()
    prepend_opencode_path()
    parser = argparse.ArgumentParser(
        description="OSM exe launcher. Default: this window is the backend, "
        "and a second window is the frontend. Does not use start.bat / start.sh."
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backend-only",
        action="store_true",
        help="This window is the manager only (do not open a frontend window)",
    )
    mode.add_argument(
        "--frontend-only",
        action="store_true",
        help="This window is the SPA proxy only (used by the second console)",
    )
    args = parser.parse_args(argv)
    try:
        if args.frontend_only:
            set_console_title(FRONTEND_TITLE)
            prepared = prepare_frontend(
                frontend_host=args.frontend_host,
                frontend_port=args.frontend_port,
            )
            print_frontend_banner(prepared)
            return asyncio.run(serve_frontend(prepared))
        set_console_title(BACKEND_TITLE)
        prepared = prepare_backend(
            frontend_host=args.frontend_host,
            frontend_port=args.frontend_port,
        )
        print_backend_banner(prepared, frontend_window=not args.backend_only)
        return asyncio.run(
            serve_backend(prepared, spawn_frontend=not args.backend_only)
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
