"""Free the manager listen port. Used by the exe before bind."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from typing import List, Optional, Set

from opencode_manager.cleanup.kill import kill_pid, may_kill
from opencode_manager.log import get_logger

logger = get_logger()

_SS_PID = re.compile(r"pid=(\d+)")


def local_port(addr: str) -> Optional[int]:
    text = (addr or "").strip()
    if not text:
        return None
    if text.startswith("["):
        _, _, rest = text.rpartition("]:")
    else:
        _, _, rest = text.rpartition(":")
    if not rest.isdigit():
        return None
    return int(rest)


def parse_netstat_listening_pids(output: str, port: int) -> List[int]:
    pids: Set[int] = set()
    for raw in output.splitlines():
        parts = raw.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if local_port(parts[1]) != port:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pids)


def parse_ss_listening_pids(output: str, port: int) -> List[int]:
    pids: Set[int] = set()
    for raw in output.splitlines():
        if "LISTEN" not in raw.upper():
            continue
        if f":{port} " not in f"{raw} " and f"]:{port} " not in f"{raw} ":
            # still accept :4096 users:(...)
            if not re.search(rf":{port}(?:\s|$)", raw):
                continue
        for match in _SS_PID.finditer(raw):
            pids.add(int(match.group(1)))
    return sorted(pids)


def port_is_busy(host: str, port: int) -> bool:
    targets = ["0.0.0.0", "127.0.0.1"] if host in {"0.0.0.0", "", "::"} else [host]
    for target in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((target, int(port)))
        except OSError:
            return True
        finally:
            sock.close()
    return False


def pids_listening_on(port: int) -> List[int]:
    port = int(port)
    if os.name == "nt":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            check=False,
        )
        return parse_netstat_listening_pids(result.stdout or "", port)
    result = subprocess.run(
        ["ss", "-lptn", f"sport = :{port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return parse_ss_listening_pids(result.stdout or "", port)
    return []


def free_listen_port(host: str, port: int, *, wait_seconds: float = 3.0) -> List[int]:
    """Kill LISTENING holders of host:port. Never this process or ancestors."""
    port = int(port)
    if not port_is_busy(host, port):
        return []
    holders = pids_listening_on(port)
    killed: List[int] = []
    for pid in holders:
        if not may_kill(pid):
            logger.warning("listen port %s held by protected pid %s; not killing", port, pid)
            continue
        logger.warning("listen port %s busy; killing leftover listener pid %s", port, pid)
        kill_pid(pid)
        killed.append(pid)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while time.monotonic() < deadline:
        if not port_is_busy(host, port):
            return killed
        time.sleep(0.1)
    return killed
