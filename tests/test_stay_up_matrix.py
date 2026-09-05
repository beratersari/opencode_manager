"""Many good and bad inbound/worker paths: the manager must stay up.

Each case marks the job or returns 400/404/409. None of them may take
down /api/meta or /api/jobs. This is the crash class from Windows
job-end after a missing branch / placeholder -1.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.git.clone import GitError
from opencode_manager.models import JobRecord
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal


class FakeRunner:
    def __init__(self, result: Terminal | None = None) -> None:
        self.calls: List[str] = []
        self.result = result or Terminal(200, "assistant says hi")

    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        self.calls.append(job.job_id)
        job.session_id = job.session_id or "ses_fake"
        job.text = self.result.text
        return self.result


class BoomRunner:
    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        raise RuntimeError("runner exploded mid-job")


def _ok(**overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "PROJ-1",
        "callback_url": "",
    }
    data.update(overrides)
    return data


def _wait_job(client: TestClient, job_id: str, *, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        if res.status_code == 200:
            last = res.json()["job"]
            if last.get("status") not in {"queued", "running"}:
                return last
        time.sleep(0.04)
    raise AssertionError(f"job {job_id} still live: {last}")


def _alive(client: TestClient) -> None:
    assert client.get("/api/meta").status_code == 200
    listing = client.get("/api/jobs?page=1&page_size=25")
    assert listing.status_code == 200
    assert "jobs" in listing.json()
    assert client.get("/api/queue").status_code == 200


BAD_JOBS: List[Dict[str, Any]] = [
    {"label": "empty body", "json": {}},
    {"label": "not an object", "json": []},
    {"label": "missing prompt", "json": _ok(), "drop": "prompt"},
    {"label": "ssh git@", "json": _ok(repo_url="git@host:g/r.git")},
    {"label": "ssh scheme", "json": _ok(repo_url="ssh://git@host/g/r.git")},
    {"label": "ftp repo", "json": _ok(repo_url="ftp://host/r.git")},
    {"label": "bad model", "json": _ok(model="nopath")},
    {"label": "empty model", "json": _ok(model="")},
    {"label": "agent plan", "json": _ok(agent_mode="plan")},
    {"label": "agent build", "json": _ok(agent_mode="build")},
    {"label": "agent Plan", "json": _ok(agent_mode="Plan")},
    {"label": "agent wizard", "json": _ok(agent_mode="wizard")},
    {"label": "agent blank", "json": _ok(agent_mode="  ")},
    {"label": "jira slash", "json": _ok(jira_id="PROJ/99")},
    {"label": "jira dot", "json": _ok(jira_id=".")},
    {"label": "jira space", "json": _ok(jira_id="a b")},
    {"label": "timeout zero", "json": _ok(timeout_in_seconds=0)},
    {"label": "timeout text", "json": _ok(timeout_in_seconds="nope")},
    {"label": "bad callback", "json": _ok(callback_url="not-a-url")},
    {"label": "ftp callback", "json": _ok(callback_url="ftp://x/y")},
    {"label": "only working_mode", "json": _ok(working_mode="Plan"), "drop": "agent_mode"},
]


def _payload(spec: Dict[str, Any]) -> Any:
    body = spec["json"]
    if isinstance(body, dict):
        body = dict(body)
        drop = spec.get("drop")
        if drop:
            body.pop(drop, None)
    return body


def test_bad_inbound_never_kills_the_app(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        _alive(client)
        for spec in BAD_JOBS:
            res = client.post("/jobs", json=_payload(spec))
            assert res.status_code == 400, spec["label"]
            env = res.json()
            assert env.get("status_code") == 400
            assert env.get("job_id") == ""
            _alive(client)
        good = client.post("/jobs", json=_ok(jira_id="AFTER-BAD"))
        assert good.status_code == 202
        _wait_job(client, good.json()["job_id"])
        _alive(client)


def test_session_delete_bad_bodies_keep_app_up(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        for body in (
            {},
            {"jira_id": "PROJ-1"},
            {"session_id": "ses_abc"},
            {"jira_id": "PROJ-1", "session_id": "-1"},
            {"jira_id": "PROJ-1", "session_id": "not-ses"},
            {"jira_id": "../x", "session_id": "ses_abc"},
            {"jira_id": "", "session_id": "ses_abc"},
        ):
            res = client.request("DELETE", "/sessions", json=body)
            assert res.status_code in {400, 503}
            _alive(client)


def test_unknown_and_malformed_gets_keep_app_up(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        assert client.get("/jobs/nope").status_code == 404
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.get("/api/jobs/nope/chat").status_code == 404
        assert client.get("/api/jobs/nope/prompts").status_code == 404
        assert client.get("/api/jobs/nope/logs").status_code == 404
        assert client.get("/api/jobs?page=-1").status_code in {200, 422}
        assert client.post("/jobs", content=b"{", headers={"content-type": "application/json"}).status_code in {
            400,
            422,
        }
        _alive(client)


def test_good_job_then_409_then_dashboard(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        first = client.post("/jobs", json=_ok(jira_id="LIVE-1", session_id="-1"))
        assert first.status_code == 202
        assert first.json()["session_id"] == ""
        second = client.post("/jobs", json=_ok(jira_id="LIVE-1"))
        assert second.status_code == 409
        assert second.json()["job_id"]
        job = _wait_job(client, first.json()["job_id"])
        assert job["status"] == "success"
        for path in (
            f"/api/jobs/{job['job_id']}",
            f"/api/jobs/{job['job_id']}/chat",
            f"/api/jobs/{job['job_id']}/prompts",
            f"/api/jobs/{job['job_id']}/logs",
        ):
            assert client.get(path).status_code == 200
        _alive(client)
        again = client.post("/jobs", json=_ok(jira_id="LIVE-1"))
        assert again.status_code == 202
        _wait_job(client, again.json()["job_id"])
        _alive(client)


@pytest.mark.parametrize("agent", ["planner", "orchestrator"])
def test_both_real_agents_succeed(tmp_settings: Settings, agent: str) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id=f"AG-{agent[:4]}", agent_mode=agent))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "success"
        assert job["agent_mode"] == agent
        _alive(client)


def test_missing_remote_branch_is_error_not_crash(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post(
            "/jobs",
            json=_ok(jira_id="PROJ-12881", source_branch="no-such-branch"),
        )
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "not_found"
        text = (job.get("error_message") or job.get("text") or "").lower()
        assert "source_branch" in text or "does not exist" in text
        poll = client.get(f"/jobs/{res.json()['job_id']}")
        assert poll.status_code == 200
        assert poll.json()["status_code"] == 404
        _alive(client)
        nxt = client.post("/jobs", json=_ok(jira_id="PROJ-12881", source_branch="develop"))
        assert nxt.status_code == 202
        _wait_job(client, nxt.json()["job_id"])
        _alive(client)


def test_git_dns_fail_is_error_not_crash(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencode_manager.worker.ls_remote_has_branch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            GitError("git failed (128): Could not resolve host: gitlabent.company.com.tr")
        ),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="DNS-1"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "error"
        assert "git" in (job.get("error_message") or job.get("text") or "").lower()
        _alive(client)


def test_runner_exception_is_error_not_crash(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=BoomRunner())
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="BOOM-1"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "error"
        assert "exploded" in (job.get("error_message") or job.get("text") or "")
        _alive(client)
        nxt = client.post("/jobs", json=_ok(jira_id="BOOM-2"))
        assert nxt.status_code == 202
        _wait_job(client, nxt.json()["job_id"])
        _alive(client)


def test_source_branch_placeholder_is_accepted(tmp_settings: Settings) -> None:
    runner = FakeRunner()
    app = create_app(tmp_settings, runner=runner)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="PROJ-12881", source_branch="-1"))
        assert res.status_code == 202
        job_id = res.json()["job_id"]
        assert job_id.startswith("job_")
        job = _wait_job(client, job_id)
        assert job["source_branch"] == ""
        assert runner.calls
        _alive(client)


def test_cleanup_helpers_raising_still_marks_job(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.ls_remote_has_branch", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.reap_path",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reap")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_file_holders",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("holders")),
    )
    monkeypatch.setattr(
        "opencode_manager.cleanup.end.kill_job_tree",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("tree")),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="HOLD-1", source_branch="missing"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "not_found"
        _alive(client)
