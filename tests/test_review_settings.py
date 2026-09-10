"""Review settings overlay and dashboard GET/PUT /api/settings."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class N8nRunner:
    def run(self, job, *, should_stop):  # noqa: ANN001, ARG002
        return Terminal(200, "ok")


def test_get_and_put_review_settings(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        got = client.get("/api/settings")
        assert got.status_code == 200
        body = got.json()
        assert body["review_agent"] == "code-reviewer"
        assert "/" in body["review_model"]
        assert body["review_timeout_seconds"] >= 1
        assert "code-reviewer" in body["agents"]
        saved = client.put(
            "/api/settings",
            json={
                "review_model": "opencode/big-pickle",
                "review_timeout_seconds": 900,
                "review_agent": "code-reviewer",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["review_timeout_seconds"] == 900
        again = client.get("/api/settings")
        assert again.json()["review_timeout_seconds"] == 900
        disk = tmp_settings.data_dir / "settings.json"
        assert disk.is_file()
        assert "review_agent" in disk.read_text(encoding="utf-8")


def test_put_settings_rejects_bad_model(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=N8nRunner())
    with TestClient(app) as client:
        bad = client.put(
            "/api/settings",
            json={"review_model": "not-a-provider-id", "review_timeout_seconds": 60, "review_agent": "code-reviewer"},
        )
        assert bad.status_code == 400


def test_packaging_local_templates_list_review_and_auth_fields() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("packaging/settings.local.windows.yaml", "packaging/settings.local.linux.yaml"):
        text = (root / name).read_text(encoding="utf-8")
        for key in (
            "review_agent",
            "review_model",
            "gitlab_token",
            "azure_url",
            "dashboard_token",
        ):
            assert key in text, f"{name} missing {key}"
