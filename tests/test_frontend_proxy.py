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
