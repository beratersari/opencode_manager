"""Real processes: two serves get different ports; a dead child is not healthy."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from opencode_manager.cleanup.kill import kill_pid
from opencode_manager.opencode import serve as serve_mod
from opencode_manager.opencode.serve import start_serve, stop_serve

_STUB = r"""
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

def _port() -> int:
    argv = sys.argv
    if "--port" in argv:
        return int(argv[argv.index("--port") + 1])
    return 0

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        return

    def do_GET(self):
        if "/global/health" in self.path:
            body = b'{"healthy": true, "who": "stub"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

class Server(HTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return HTTPServer.server_bind(self)

try:
    Server(("127.0.0.1", _port()), Handler).serve_forever()
except OSError:
    raise SystemExit(2)
"""


def _launcher(tmp_path: Path) -> Path:
    stub = tmp_path / "fake_opencode.py"
    stub.write_text(_STUB, encoding="utf-8")
    py = sys.executable
    if os.name == "nt":
        bat = tmp_path / "fake_opencode.cmd"
        bat.write_text(
            f'@echo off\r\n"{py}" "{stub}" %*\r\n',
            encoding="utf-8",
        )
        return bat
    sh = tmp_path / "fake_opencode"
    sh.write_text(f'#!/bin/sh\nexec "{py}" "{stub}" "$@"\n', encoding="utf-8")
    sh.chmod(sh.stat().st_mode | 0o111)
    return sh


def _bind_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_two_real_serves_get_distinct_ports_and_own_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = str(_launcher(tmp_path))
    real_popen = serve_mod.subprocess.Popen

    def popen(cmd, **kwargs):  # noqa: ANN001, ANN003
        argv = list(cmd)
        if argv and str(argv[0]).endswith((".cmd", ".bat")):
            argv = [sys.executable, str(tmp_path / "fake_opencode.py"), *argv[1:]]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(serve_mod.subprocess, "Popen", popen)
    handles: list = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        try:
            cwd = tmp_path / name
            cwd.mkdir()
            handle = start_serve(
                bin_name=binary,
                cwd=cwd,
                log_path=tmp_path / f"{name}.log",
                timeout=8.0,
            )
            handles.append(handle)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("job_a",)),
        threading.Thread(target=worker, args=("job_b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    try:
        assert errors == [], errors
        assert len(handles) == 2
        ports = {h.port for h in handles}
        pids = {h.pid for h in handles}
        assert len(ports) == 2, ports
        assert len(pids) == 2, pids
        for handle in handles:
            assert handle.proc.poll() is None
            res = httpx.get(f"{handle.base_url}/global/health", timeout=2.0)
            assert res.status_code == 200
            assert res.json()["who"] == "stub"
    finally:
        for handle in handles:
            stop_serve(handle)


def _start_many(tmp_path: Path, binary: str, n: int, *, timeout: float) -> list:
    handles: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            cwd = tmp_path / f"job_{i}"
            cwd.mkdir()
            handle = start_serve(
                bin_name=binary,
                cwd=cwd,
                log_path=tmp_path / f"job_{i}.log",
                timeout=timeout,
            )
            with lock:
                handles.append(handle)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 30)
    if errors:
        for handle in handles:
            stop_serve(handle)
        raise AssertionError(errors)
    return handles


def test_sixteen_concurrent_serves_all_get_unique_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = str(_launcher(tmp_path))
    real_popen = serve_mod.subprocess.Popen

    def popen(cmd, **kwargs):  # noqa: ANN001, ANN003
        argv = list(cmd)
        if argv and str(argv[0]).endswith((".cmd", ".bat")):
            argv = [sys.executable, str(tmp_path / "fake_opencode.py"), *argv[1:]]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(serve_mod.subprocess, "Popen", popen)
    handles = _start_many(tmp_path, binary, 16, timeout=12.0)
    try:
        ports = [h.port for h in handles]
        pids = [h.pid for h in handles]
        assert len(handles) == 16
        assert len(set(ports)) == 16, ports
        assert len(set(pids)) == 16, pids
        for handle in handles:
            assert handle.proc.poll() is None
            res = httpx.get(f"{handle.base_url}/global/health", timeout=2.0)
            assert res.status_code == 200
    finally:
        for handle in handles:
            stop_serve(handle)


def test_occupied_port_does_not_count_as_this_child_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old bug: child loses bind, exits, wait_health still 200 from occupier."""
    binary = str(_launcher(tmp_path))
    occupied = _bind_port()
    occupier = subprocess.Popen(
        [sys.executable, str(tmp_path / "fake_opencode.py"), "serve", "--port", str(occupied)],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # launcher writes fake_opencode.py
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{occupied}/global/health", timeout=0.3).status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)
        else:
            raise AssertionError("occupier never became healthy")

        monkeypatch.setattr(serve_mod, "free_port", lambda: occupied)
        real_popen = serve_mod.subprocess.Popen

        def popen(cmd, **kwargs):  # noqa: ANN001, ANN003
            argv = list(cmd)
            if argv and str(argv[0]).endswith((".cmd", ".bat")):
                argv = [sys.executable, str(tmp_path / "fake_opencode.py"), *argv[1:]]
            return real_popen(argv, **kwargs)

        monkeypatch.setattr(serve_mod.subprocess, "Popen", popen)
        cwd = tmp_path / "job_collide"
        cwd.mkdir()
        log_path = tmp_path / "collide.log"
        probe = subprocess.run(
            [sys.executable, str(tmp_path / "fake_opencode.py"), "serve", "--port", str(occupied)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert probe.returncode == 2, (
            f"second bind should fail: rc={probe.returncode} out={probe.stdout!r} err={probe.stderr!r}"
        )
        with pytest.raises(RuntimeError, match="exited"):
            start_serve(
                bin_name=binary,
                cwd=cwd,
                log_path=log_path,
                timeout=3.0,
            )
        # Occupier must still be the one answering. We must not have
        # adopted its 200 as this job's serve.
        still = httpx.get(f"http://127.0.0.1:{occupied}/global/health", timeout=2.0)
        assert still.status_code == 200
        assert occupier.poll() is None
    finally:
        kill_pid(occupier.pid)
        occupier.wait(timeout=5)


@pytest.mark.skipif(not shutil.which("opencode"), reason="opencode not on PATH")
def test_eight_concurrent_opencode_serves_unique_ports(tmp_path: Path) -> None:
    handles = _start_many(tmp_path, "opencode", 8, timeout=45.0)
    try:
        ports = [h.port for h in handles]
        pids = [h.pid for h in handles]
        assert len(handles) == 8
        assert len(set(ports)) == 8, ports
        assert len(set(pids)) == 8, pids
        for handle in handles:
            assert handle.proc.poll() is None
            res = httpx.get(f"{handle.base_url}/global/health", timeout=3.0)
            assert res.status_code == 200
            assert isinstance(res.json(), dict)
    finally:
        for handle in handles:
            stop_serve(handle)


@pytest.mark.skipif(not shutil.which("opencode"), reason="opencode not on PATH")
def test_two_real_opencode_serves_get_distinct_ports(tmp_path: Path) -> None:
    handles = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        try:
            cwd = tmp_path / name
            cwd.mkdir()
            handles.append(
                start_serve(
                    bin_name="opencode",
                    cwd=cwd,
                    log_path=tmp_path / f"{name}.log",
                    timeout=30.0,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("oc_a",)),
        threading.Thread(target=worker, args=("oc_b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert errors == [], errors
        assert len(handles) == 2
        assert len({h.port for h in handles}) == 2
        assert len({h.pid for h in handles}) == 2
        for handle in handles:
            assert handle.proc.poll() is None
            res = httpx.get(f"{handle.base_url}/global/health", timeout=3.0)
            assert res.status_code == 200
            assert isinstance(res.json(), dict)
    finally:
        for handle in handles:
            stop_serve(handle)
