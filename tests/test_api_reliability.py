"""Real POST /jobs API: 200 mixed-difficulty jobs, measure error rates."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.settings import Settings
from tests.test_api import FakeRunner

ROOT = Path(__file__).resolve().parents[1]


def _rel():
    spec = importlib.util.spec_from_file_location(
        "osm_reliability", ROOT / "tester" / "reliability.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _wait_terminal(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        res = client.get(f"/jobs/{job_id}")
        last = res.json()
        if res.status_code == 404:
            return last
        if not last.get("live") and last.get("status_code") not in (None, 202):
            return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still live: {last}")


def test_api_reliability_200_jobs_via_post_jobs(tmp_settings: Settings) -> None:
    rel = _rel()
    tmp_settings.max_concurrent_jobs = 4
    recipes = rel.pick_recipes(200)
    assert len(recipes) == 200
    diffs = {r.difficulty for r in recipes}
    assert diffs >= {"easy", "medium", "hard", "planner", "invalid"}
    app = create_app(tmp_settings, runner=FakeRunner())
    report = rel.Report(run_id="test", count=200)
    with TestClient(app) as client:
        assert client.get("/api/meta").status_code == 200
        pending: list = []
        for i, recipe in enumerate(recipes, start=1):
            body = rel.job_body(
                recipe,
                index=i,
                run_id="T200",
                repo_url="https://gitlab.example/g/r.git",
            )
            res = client.post("/jobs", json=body)
            row = rel.Row(
                index=i,
                jira_id=body["jira_id"],
                difficulty=recipe.difficulty,
                inbound_http=res.status_code,
                job_id=str((res.json() or {}).get("job_id") or ""),
            )
            report.add_inbound(row)
            if res.status_code == 202:
                assert row.job_id.startswith("job_")
                pending.append(row)
            elif recipe.inbound == "invalid":
                assert res.status_code == 400
            else:
                raise AssertionError(f"unexpected {res.status_code} for {recipe.name} {res.text}")
        assert len(pending) >= 150
        for row in pending:
            got = _wait_terminal(client, row.job_id)
            row.terminal_code = got.get("status_code")
            row.status = str(got.get("status") or "")
            report.add_terminal(row)
            assert row.terminal_code == 200, row
        meta = client.get("/api/meta")
        listing = client.get("/api/jobs?page=1&page_size=25")
        assert meta.status_code == 200
        assert listing.status_code == 200
        assert listing.json()["total"] >= len(pending)
    assert report.accepted() == len(pending)
    assert report.inbound_error_rate() > 0
    assert report.job_error_rate() == 0
    assert report.inbound["400"] >= 20
    assert report.terminal["200"] == len(pending)


def test_reliability_catalog_has_unique_jira_ids() -> None:
    rel = _rel()
    seen = set()
    for i, recipe in enumerate(rel.pick_recipes(200), start=1):
        jira = rel.job_body(recipe, index=i, run_id="UID", repo_url="https://x/y.git")["jira_id"]
        assert jira not in seen
        seen.add(jira)
    assert len(seen) == 200
