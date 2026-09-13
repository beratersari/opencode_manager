"""attach_spa on listen_port must not serve files outside web/dist."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencode_manager.api import attach_spa, spa_file_for
from opencode_manager.app import create_app
from opencode_manager.settings import Settings


def _dist_with_secret(root: Path) -> tuple[Path, Path]:
    dist = root / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    secret = root / "settings.local.yaml"
    secret.write_text(
        "dashboard_password: SUPERSECRET\ndashboard_token: glpat-LEAK\n",
        encoding="utf-8",
    )
    return dist, secret


def test_spa_file_for_rejects_parent_and_absolute(tmp_path: Path) -> None:
    dist, secret = _dist_with_secret(tmp_path)
    assert spa_file_for(dist, "favicon.svg").name == "favicon.svg"
    assert spa_file_for(dist, "jobs/job_abc").name == "index.html"
    assert spa_file_for(dist, "../settings.local.yaml").name == "index.html"
    assert spa_file_for(dist, "..\\settings.local.yaml").name == "index.html"
    assert spa_file_for(dist, "../../settings.local.yaml").name == "index.html"
    assert spa_file_for(dist, str(secret)).name == "index.html"
    assert spa_file_for(dist, "").name == "index.html"


def test_attach_spa_percent_encoded_dotdot_is_index(tmp_path: Path) -> None:
    dist, _secret = _dist_with_secret(tmp_path)
    app = FastAPI()
    attach_spa(app, dist)
    client = TestClient(app)
    for path in (
        "/..%2F..%2Fsettings.local.yaml",
        "/..%5C..%5Csettings.local.yaml",
        "/..\\..\\settings.local.yaml",
        "/../../settings.local.yaml",
        "/web/../settings.local.yaml",
        "/%2e%2e/%2e%2e/settings.local.yaml",
    ):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "SUPERSECRET" not in res.text, path
        assert "glpat-LEAK" not in res.text, path
        assert "spa" in res.text, path


def test_attach_spa_still_serves_real_dist_files(tmp_path: Path) -> None:
    dist, _secret = _dist_with_secret(tmp_path)
    app = FastAPI()
    attach_spa(app, dist)
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    assert "spa" in index.text
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200
    assert "<svg" in icon.text
    missing = client.get("/jobs/job_missing")
    assert missing.status_code == 200
    assert "spa" in missing.text


def test_spa_file_for_nested_asset_stays_inside_dist(tmp_path: Path) -> None:
    dist, _secret = _dist_with_secret(tmp_path)
    nested = spa_file_for(dist, "assets/app.js")
    assert nested.name == "app.js"
    nested.resolve().relative_to(dist.resolve())


def test_create_app_spa_does_not_leak_overlay_without_auth(tmp_settings: Settings, tmp_path: Path) -> None:
    dist, _secret = _dist_with_secret(tmp_path)
    tmp_settings.project_root = tmp_path
    tmp_settings.dashboard_user = "berat"
    tmp_settings.dashboard_password = "s3cret"
    tmp_settings.dashboard_token = "n8n-secret"
    assert (tmp_path / "web" / "dist").is_dir()
    assert dist.is_dir()
    with TestClient(create_app(tmp_settings)) as client:
        leaked = client.get("/..%2F..%2Fsettings.local.yaml")
        assert leaked.status_code == 200
        assert "SUPERSECRET" not in leaked.text
        assert "n8n-secret" not in leaked.text
        assert "s3cret" not in leaked.text
        meta = client.get("/api/meta")
        assert meta.status_code == 401
        poller = client.get("/jobs/job_missing")
        assert poller.status_code == 401


def test_poller_jobs_id_stays_json_when_spa_attached(tmp_settings: Settings, tmp_path: Path) -> None:
    _dist_with_secret(tmp_path)
    tmp_settings.project_root = tmp_path
    with TestClient(create_app(tmp_settings)) as client:
        res = client.get("/jobs/job_missing")
        assert res.status_code == 404
        ctype = (res.headers.get("content-type") or "").lower()
        assert "json" in ctype
        assert "html" not in ctype
        assert res.json()["status_code"] == 404
