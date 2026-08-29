"""Start / stop one opencode serve per job."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.log import get_logger

logger = get_logger()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
) -> ServeHandle:
    binary = shutil.which(bin_name) or bin_name
    port = free_port()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = ""
    proc = subprocess.Popen(
        [
            binary,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--print-logs",
            "--log-level",
            "INFO",
        ],
        cwd=str(cwd),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    proc._om_log_f = log_f  # type: ignore[attr-defined]
    base = f"http://127.0.0.1:{port}"
    wait_health(base, str(cwd), timeout=timeout)
    logger.info("opencode serve pid=%s port=%s", proc.pid, port)
    return ServeHandle(pid=int(proc.pid), port=port, base_url=base, proc=proc, log_path=log_path)


def wait_health(base_url: str, directory: str, *, timeout: float) -> dict:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    headers = {"x-opencode-directory": directory}
    with httpx.Client(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(base_url.rstrip("/") + "/global/health", headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict):
                        return data
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
    raise TimeoutError(f"serve health not ready at {base_url}: {last}")


def stop_serve(handle: Optional[ServeHandle]) -> None:
    if handle is None:
        return
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
