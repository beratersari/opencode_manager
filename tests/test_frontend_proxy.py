"""Tests for the offline dashboard SPA proxy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from opencode_manager.dashboard import frontend_proxy as mod


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    (d / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    assets = d / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    return d


def test_spa_index_served(dist: Path) -> None:
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:4096")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="root"' in r.text


def test_jobs_route_is_spa(dist: Path) -> None:
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:4096")
    client = TestClient(app)
    r = client.get("/jobs/job_abc")
    assert r.status_code == 200
    assert 'id="root"' in r.text


def test_js_assets_not_served_as_text_plain(dist: Path) -> None:
    import mimetypes

    mimetypes.init()
    mimetypes.types_map[".js"] = "text/plain"

    app = mod.build_app(dist=dist, backend="http://127.0.0.1:4096")
    client = TestClient(app)
    r = client.get("/assets/app.js")
    assert r.status_code == 200
    ct = (r.headers.get("content-type") or "").lower()
    assert "javascript" in ct, f"expected JS MIME, got {ct!r}"
    assert "text/plain" not in ct


def test_backend_ws_headers_copy_cookie_and_bearer() -> None:
    assert mod.backend_ws_headers({}) == {}
    assert mod.backend_ws_headers({"cookie": "amir_mini_session=abc"}) == {
        "Cookie": "amir_mini_session=abc"
    }
    assert mod.backend_ws_headers({"authorization": "Bearer amir-mini-n8n"}) == {
        "Authorization": "Bearer amir-mini-n8n"
    }
    assert mod.backend_ws_headers({"x-amir-mini-token": "amir-mini-n8n"}) == {
        "X-Amir-Mini-Token": "amir-mini-n8n"
    }


def test_proxy_ws_forwards_session_cookie(dist: Path, tmp_settings) -> None:
    import threading
    import time

    import uvicorn
    from opencode_manager.app import create_app
    from opencode_manager.dashboard.auth import SESSION_COOKIE
    from opencode_manager.worker import Terminal

    class N8nRunner:
        def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
            return Terminal(200, "ok")

    tmp_settings.dashboard_password = "amir-mini"
    tmp_settings.dashboard_token = "amir-mini-n8n"
    backend_app = create_app(tmp_settings, runner=N8nRunner())
    server = uvicorn.Server(uvicorn.Config(backend_app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    front = mod.build_app(dist=dist, backend=f"http://127.0.0.1:{port}")
    try:
        with TestClient(front) as client:
            login = client.post("/api/login", json={"username": "", "password": "amir-mini"})
            assert login.status_code == 200
            assert login.cookies.get(SESSION_COOKIE)
            with client.websocket_connect("/ws") as ws:
                data = ws.receive_json()
            assert "running" in data
            assert "queue_queued" in data
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_proxy_api_returns_502_when_backend_down(dist: Path) -> None:
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:9")
    client = TestClient(app)

    class BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    with patch("opencode_manager.dashboard.frontend_proxy.httpx.AsyncClient", return_value=BoomClient()):
        r = client.get("/api/meta")
    assert r.status_code == 502
    assert "Backend unreachable" in r.text
