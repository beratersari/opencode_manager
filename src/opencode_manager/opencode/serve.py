"""Start / stop one opencode serve per job."""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Set

import httpx

from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.log import clip, get_logger, log_command, log_fail, redact

logger = get_logger()


def serve_log_path(serve_dir: Path, job_id: str) -> Path:
    """Stable per-job serve stdout/stderr: `{serve_dir}/{job_id}.log`."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (job_id or "")) or "job"
    return Path(serve_dir) / f"{safe}.log"


def read_serve_log(path: Path) -> str:
    """Whole serve log, redacted. Empty if the file is missing."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return redact(text)


_port_lock = threading.Lock()
_reserved_ports: Set[int] = set()


def free_port() -> int:
    """Ask the OS for an unused port. The socket is closed before return."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def reserve_port() -> int:
    """Ephemeral port that no other OSM serve start may pick until released.

    Closes the probe socket so the child can bind. Two workers in this
    process cannot draw the same number in that gap.
    """
    with _port_lock:
        for _ in range(64):
            port = free_port()
            if port not in _reserved_ports:
                _reserved_ports.add(port)
                return port
    raise RuntimeError("could not reserve a free localhost port")


def release_port(port: Optional[int]) -> None:
    if port is None:
        return
    with _port_lock:
        _reserved_ports.discard(int(port))


@dataclass
class ServeHandle:
    pid: int
    port: int
    base_url: str
    proc: subprocess.Popen
    log_path: Path


def start_serve(
    *,
    bin_name: str,
    cwd: Path,
    log_path: Path,
    timeout: float,
    on_spawn: Optional[Callable[[ServeHandle], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    attempt_timeout: Optional[float] = None,
    hang_timeout: Optional[float] = None,
) -> ServeHandle:
    if should_stop and should_stop():
        raise RuntimeError("manager shutting down")
    binary = shutil.which(bin_name) or bin_name
    last_exc: Optional[BaseException] = None
    for _try in range(5):
        try:
            return _start_serve_once(
                binary=binary,
                cwd=cwd,
                log_path=log_path,
                timeout=timeout,
                on_spawn=on_spawn,
                should_stop=should_stop,
                attempt_timeout=attempt_timeout,
                hang_timeout=hang_timeout,
            )
        except RuntimeError as exc:
            last_exc = exc
            if "shutting down" in str(exc).lower():
                raise
            if "exited" not in str(exc).lower() and "reserve" not in str(exc).lower():
                raise
            logger.warning("serve start retry after %s", exc)
            continue
    assert last_exc is not None
    raise last_exc


def _start_serve_once(
    *,
    binary: str,
    cwd: Path,
    log_path: Path,
    timeout: float,
    on_spawn: Optional[Callable[[ServeHandle], None]],
    should_stop: Optional[Callable[[], bool]],
    attempt_timeout: Optional[float],
    hang_timeout: Optional[float],
) -> ServeHandle:
    port = reserve_port()
    try:
        return _spawn_and_wait(
            binary=binary,
            cwd=cwd,
            log_path=log_path,
            timeout=timeout,
            on_spawn=on_spawn,
            should_stop=should_stop,
            attempt_timeout=attempt_timeout,
            hang_timeout=hang_timeout,
            port=port,
        )
    finally:
        release_port(port)


def _spawn_and_wait(
    *,
    binary: str,
    cwd: Path,
    log_path: Path,
    timeout: float,
    on_spawn: Optional[Callable[[ServeHandle], None]],
    should_stop: Optional[Callable[[], bool]],
    attempt_timeout: Optional[float],
    hang_timeout: Optional[float],
    port: int,
) -> ServeHandle:
    cmd = [
        binary,
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
        "--print-logs",
        "--log-level",
        "INFO",
    ]
    logger.info(
        "opencode command: %s cwd=%s attempt_timeout=%ss health_timeout=%ss hang_timeout=%ss log=%s",
        shlex.join(cmd),
        cwd,
        attempt_timeout if attempt_timeout is not None else "-",
        timeout,
        hang_timeout if hang_timeout is not None else "-",
        log_path,
    )
    log_command(logger, cmd, cwd=cwd, timeout=timeout, extra=f"log={log_path}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Append so outer retries keep earlier serve output for the report zip.
    log_f = open(log_path, "a", encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_f.write(f"\n===== opencode serve start {stamp} port={port} cwd={cwd} =====\n")
    log_f.flush()
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = ""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        log_f.close()
        log_fail(logger, "serve failed to start", err=exc, argv=shlex.join(cmd), cwd=cwd)
        raise
    proc._om_log_f = log_f  # type: ignore[attr-defined]
    logger.info("serve spawned pid=%s port=%s argv=%s", proc.pid, port, shlex.join(cmd))
    base = f"http://127.0.0.1:{port}"
    handle = ServeHandle(pid=int(proc.pid), port=port, base_url=base, proc=proc, log_path=log_path)
    if on_spawn is not None:
        on_spawn(handle)
    try:
        health = wait_health(
            base, str(cwd), timeout=timeout, should_stop=should_stop, proc=proc
        )
    except Exception as exc:
        tail = _serve_log_tail(log_path)
        log_fail(
            logger,
            "serve health failed; killing",
            pid=proc.pid,
            port=port,
            err=exc,
            serve_log_tail=tail,
        )
        stop_serve(handle)
        raise
    logger.info("opencode serve up pid=%s port=%s health=%s", proc.pid, port, health)
    return handle


def _serve_log_tail(path: Path, n: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def wait_health(
    base_url: str,
    directory: str,
    *,
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
    proc: Optional[subprocess.Popen] = None,
) -> dict:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    last_status: Optional[int] = None
    headers = {"x-opencode-directory": directory}
    url = base_url.rstrip("/") + "/global/health"
    logger.info("health wait GET %s timeout=%ss directory=%s", url, timeout, directory)
    with httpx.Client(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            if should_stop and should_stop():
                raise RuntimeError("manager shutting down")
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    raise RuntimeError(
                        f"serve process exited rc={rc} before health at {base_url}"
                    )
            try:
                response = client.get(url, headers=headers)
                last_status = response.status_code
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict):
                        return data
                last = Exception(f"HTTP {response.status_code} body={clip(response.text, 200)}")
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
    raise TimeoutError(
        f"serve health not ready at {base_url} last_status={last_status} last={last}"
    )


def stop_serve(handle: Optional[ServeHandle]) -> None:
    if handle is None:
        logger.info("stop serve: no handle")
        return
    logger.info("stop serve pid=%s port=%s", handle.pid, handle.port)
    kill_pid(handle.pid)
    try:
        handle.proc.wait(timeout=5)
    except Exception:
        kill_pid(handle.pid)
    log_f = getattr(handle.proc, "_om_log_f", None)
    if log_f is not None:
        try:
            log_f.close()
        except Exception:
            pass
