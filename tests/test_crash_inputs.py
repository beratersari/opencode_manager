"""Extensive good/bad inputs through the real HTTP app.

Every case must leave /api/meta, /api/jobs, and /api/queue answering.
Worker failures go through OpenCodeRunner (real job-end), not only FakeRunner.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.git.clone import GitError, clone_path_for
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed
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


class GateRunner:
    """Hold the first job until release so 409 / queue can be observed live."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: List[str] = []

    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        self.calls.append(job.job_id)
        self.entered.set()
        self.release.wait(timeout=8)
        job.session_id = job.session_id or "ses_gate"
        job.text = "done after gate"
        return Terminal(200, job.text)


class BoomRunner:
    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        raise RuntimeError(f"runner exploded for {job.jira_id}")


class StatusRunner:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text

    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        job.text = self.text
        return Terminal(self.status, self.text)


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


def _wait_job(client: TestClient, job_id: str, *, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        if res.status_code == 200:
            last = res.json()["job"]
            if last.get("status") not in {"queued", "running"}:
                return last
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} still live: {last}")


def _alive(client: TestClient) -> None:
    assert client.get("/api/meta").status_code == 200
    listing = client.get("/api/jobs?page=1&page_size=25")
    assert listing.status_code == 200
    assert "jobs" in listing.json()
    assert client.get("/api/queue").status_code == 200
    assert client.get("/api/report-context").status_code == 200


def _dashboard(client: TestClient, job_id: str) -> None:
    for path in (
        f"/api/jobs/{job_id}",
        f"/api/jobs/{job_id}/chat",
        f"/api/jobs/{job_id}/prompts",
        f"/api/jobs/{job_id}/logs",
        f"/api/jobs/{job_id}/logs?limit=0",
        f"/api/jobs/{job_id}/serve-log",
    ):
        assert client.get(path).status_code == 200, path


BAD_INBOUND: List[Dict[str, Any]] = [
    {"label": "empty object", "json": {}},
    {"label": "array body", "json": []},
    {"label": "string body", "json": "hello"},
    {"label": "number body", "json": 7},
    {"label": "null body", "json": None},
    {"label": "missing prompt", "json": _ok(), "drop": "prompt"},
    {"label": "null prompt", "json": _ok(prompt=None)},
    {"label": "blank prompt", "json": _ok(prompt="   ")},
    {"label": "ssh git@", "json": _ok(repo_url="git@host:g/r.git")},
    {"label": "ssh scheme", "json": _ok(repo_url="ssh://git@host/g/r.git")},
    {"label": "git+ssh", "json": _ok(repo_url="git+ssh://host/r.git")},
    {"label": "ftp repo", "json": _ok(repo_url="ftp://host/r.git")},
    {"label": "sftp repo", "json": _ok(repo_url="sftp://host/r.git")},
    {"label": "blank repo", "json": _ok(repo_url="  ")},
    {"label": "null repo", "json": _ok(repo_url=None)},
    {"label": "missing repo", "json": _ok(), "drop": "repo_url"},
    {"label": "model no slash", "json": _ok(model="nopath")},
    {"label": "model empty", "json": _ok(model="")},
    {"label": "model slash only", "json": _ok(model="/")},
    {"label": "model provider only", "json": _ok(model="ollama/")},
    {"label": "null model", "json": _ok(model=None)},
    {"label": "agent plan", "json": _ok(agent_mode="plan")},
    {"label": "agent build", "json": _ok(agent_mode="build")},
    {"label": "agent Plan", "json": _ok(agent_mode="Plan")},
    {"label": "agent PLANNER", "json": _ok(agent_mode="PLANNER")},
    {"label": "agent wizard", "json": _ok(agent_mode="wizard")},
    {"label": "agent general", "json": _ok(agent_mode="general")},
    {"label": "agent blank", "json": _ok(agent_mode="  ")},
    {"label": "agent null", "json": _ok(agent_mode=None)},
    {"label": "only working_mode", "json": _ok(working_mode="Plan"), "drop": "agent_mode"},
    {"label": "jira slash", "json": _ok(jira_id="PROJ/99")},
    {"label": "jira backslash", "json": _ok(jira_id="PROJ\\99")},
    {"label": "jira dot", "json": _ok(jira_id=".")},
    {"label": "jira dotdot", "json": _ok(jira_id="..")},
    {"label": "jira space", "json": _ok(jira_id="a b")},
    {"label": "jira unicode", "json": _ok(jira_id="PROJ-üğ")},
    {"label": "jira leading hyphen", "json": _ok(jira_id="-KAN-1")},
    {"label": "jira empty", "json": _ok(jira_id="")},
    {"label": "jira too long", "json": _ok(jira_id="A" + "x" * 80)},
    {"label": "timeout zero", "json": _ok(timeout_in_seconds=0)},
    {"label": "timeout negative", "json": _ok(timeout_in_seconds=-5)},
    {"label": "timeout text", "json": _ok(timeout_in_seconds="nope")},
    {"label": "timeout list", "json": _ok(timeout_in_seconds=[30])},
    {"label": "timeout false", "json": _ok(timeout_in_seconds=False)},
    {"label": "retry text", "json": _ok(retry_count="nope")},
    {"label": "retry list", "json": _ok(retry_count=[1])},
    {"label": "callback relative", "json": _ok(callback_url="not-a-url")},
    {"label": "callback ftp", "json": _ok(callback_url="ftp://x/y")},
    {"label": "callback no host", "json": _ok(callback_url="http://")},
    {"label": "callback ws", "json": _ok(callback_url="ws://127.0.0.1/x")},
    {"label": "missing timeout", "json": _ok(), "drop": "timeout_in_seconds"},
    {"label": "missing retry", "json": _ok(), "drop": "retry_count"},
    {"label": "missing jira", "json": _ok(), "drop": "jira_id"},
    {"label": "missing model", "json": _ok(), "drop": "model"},
]


GOOD_INBOUND: List[Dict[str, Any]] = [
    {"label": "orchestrator", "json": _ok(jira_id="GOOD-ORCH"), "agent": "orchestrator"},
    {"label": "planner", "json": _ok(jira_id="GOOD-PLAN", agent_mode="planner"), "agent": "planner"},
    {"label": "agent_type", "json": {**{k: v for k, v in _ok(jira_id="GOOD-TYPE").items() if k != "agent_mode"}, "agent_type": "planner"}, "agent": "planner"},
    {"label": "session -1", "json": _ok(jira_id="GOOD-SES1", session_id="-1")},
    {"label": "session empty", "json": _ok(jira_id="GOOD-SESE", session_id="")},
    {"label": "session uuid", "json": _ok(jira_id="GOOD-UUID", session_id="550e8400-e29b-41d4-a716-446655440000")},
    {"label": "session ses_", "json": _ok(jira_id="GOOD-SES", session_id="ses_abc123"), "session": "ses_abc123"},
    {"label": "unicode prompt", "json": _ok(jira_id="GOOD-UNI", prompt="Türkçe plan: geliştir")},
    {"label": "long prompt", "json": _ok(jira_id="GOOD-LONG", prompt="x" * 50_000)},
    {"label": "feature branch", "json": _ok(jira_id="GOOD-FEAT", source_branch="feature/KAN-9")},
    {"label": "missing branch", "json": _ok(jira_id="GOOD-NOBR"), "drop": "source_branch"},
    {"label": "branch -1", "json": _ok(jira_id="GOOD-BR1", source_branch="-1")},
    {"label": "branch empty", "json": _ok(jira_id="GOOD-BRE", source_branch="")},
    {"label": "branch spaces", "json": _ok(jira_id="GOOD-BRS", source_branch="   ")},
    {"label": "branch null", "json": _ok(jira_id="GOOD-BRN", source_branch=None)},
    {"label": "retry zero clamped", "json": _ok(jira_id="GOOD-R0", retry_count=0)},
    {"label": "timeout string", "json": _ok(jira_id="GOOD-TS", timeout_in_seconds="45")},
    {"label": "file repo", "json": _ok(jira_id="GOOD-FILE", repo_url="file:///tmp/repo.git")},
    {"label": "https callback", "json": _ok(jira_id="GOOD-CB", callback_url="https://n8n.example/wait/abc")},
    {"label": "extra fields", "json": _ok(jira_id="GOOD-XTRA", working_mode="Plan", PAT="secret-token", foo={"bar": 1})},
    {"label": "dotted jira", "json": _ok(jira_id="PROJ.1_2-3")},
    {"label": "model extra slash", "json": _ok(jira_id="GOOD-MDL", model="ollama/org/Restricted-Kimi-K2.6")},
    {"label": "timeout true", "json": _ok(jira_id="GOOD-T1", timeout_in_seconds=True)},
]


def _payload(spec: Dict[str, Any]) -> Any:
    body = spec["json"]
    if isinstance(body, dict):
        body = dict(body)
        drop = spec.get("drop")
        if drop:
            body.pop(drop, None)
    return body


def test_bad_inbound_matrix_never_kills_app(tmp_settings: Settings) -> None:
    runner = FakeRunner()
    app = create_app(tmp_settings, runner=runner)
    with TestClient(app) as client:
        _alive(client)
        for spec in BAD_INBOUND:
            res = client.post("/jobs", json=_payload(spec))
            assert res.status_code == 400, spec["label"]
            env = res.json()
            assert env.get("status_code") == 400, spec["label"]
            assert env.get("job_id") == "", spec["label"]
            assert "PAT" not in json.dumps(env)
            _alive(client)
        assert runner.calls == []
        good = client.post("/jobs", json=_ok(jira_id="AFTER-BADS"))
        assert good.status_code == 202
        job = _wait_job(client, good.json()["job_id"])
        assert job["status"] == "success"
        _dashboard(client, job["job_id"])
        _alive(client)


def test_good_inbound_matrix_succeeds(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        for spec in GOOD_INBOUND:
            res = client.post("/jobs", json=_payload(spec))
            assert res.status_code == 202, spec["label"]
            env = res.json()
            assert env["job_id"].startswith("job_"), spec["label"]
            assert "secret-token" not in json.dumps(env)
            job = _wait_job(client, env["job_id"])
            assert job["status"] == "success", spec["label"]
            if spec.get("agent"):
                assert job["agent_mode"] == spec["agent"], spec["label"]
            if spec.get("session"):
                assert job["session_id"] == spec["session"], spec["label"]
            _dashboard(client, job["job_id"])
            _alive(client)


def test_malformed_http_bodies_keep_app_up(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        cases = [
            client.post("/jobs", content=b"{", headers={"content-type": "application/json"}),
            client.post("/jobs", content=b"\xff\xfe", headers={"content-type": "application/json"}),
            client.post("/jobs", content=b"", headers={"content-type": "application/json"}),
            client.post("/jobs", content=b"null", headers={"content-type": "application/json"}),
            client.post("/jobs", content=b'"x"', headers={"content-type": "application/json"}),
            client.post("/jobs", content=b"do work", headers={"content-type": "text/plain"}),
            client.post("/jobs"),
            client.request("DELETE", "/sessions", content=b"{"),
            client.request("DELETE", "/sessions", content=b""),
            client.request("DELETE", "/sessions"),
        ]
        for res in cases:
            assert res.status_code in {400, 422}, res.text
            _alive(client)


def test_unknown_gets_and_dashboard_writes_keep_app_up(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        for path in (
            "/jobs/nope",
            "/jobs/",
            "/jobs/%00",
            "/jobs/../jobs",
            "/jobs/job_" + "a" * 80,
            "/api/jobs/nope",
            "/api/jobs/nope/chat",
            "/api/jobs/nope/prompts",
            "/api/jobs/nope/logs",
            "/api/jobs/nope/serve-log",
        ):
            assert client.get(path).status_code in {404, 405, 422}, path
        listing = client.get("/api/jobs?page=-1")
        assert listing.status_code in {200, 422}
        assert client.get("/api/jobs?page=999").status_code == 200
        assert client.get("/api/jobs?page_size=101").status_code == 422
        assert client.get("/api/jobs?filter=not-a-filter").status_code == 200
        assert client.get("/api/jobs?filter=error&jira_id=../x").status_code == 200
        assert client.get("/api/queue?jira_id=PROJ/99").status_code == 200
        assert client.get("/api/report-context").status_code == 200
        for method, path in (
            ("POST", "/api/jobs"),
            ("POST", "/api/report-context"),
            ("PATCH", "/api/jobs/x"),
            ("PUT", "/api/jobs/x"),
            ("DELETE", "/api/jobs/x"),
        ):
            assert client.request(method, path).status_code == 405
        _alive(client)


def test_live_409_then_reuse_ticket(tmp_settings: Settings) -> None:
    runner = GateRunner()
    app = create_app(tmp_settings, runner=runner)
    with TestClient(app) as client:
        first = client.post("/jobs", json=_ok(jira_id="LIVE-1", session_id="-1"))
        assert first.status_code == 202
        assert first.json()["session_id"] == ""
        assert runner.entered.wait(timeout=5)
        poll = client.get(f"/jobs/{first.json()['job_id']}")
        assert poll.status_code == 202
        assert poll.json()["live"] is True
        assert poll.json()["status_code"] == 202
        second = client.post("/jobs", json=_ok(jira_id="LIVE-1"))
        assert second.status_code == 409
        assert second.json()["job_id"] == first.json()["job_id"]
        dead = client.request(
            "DELETE",
            "/sessions",
            json={"jira_id": "LIVE-1", "session_id": "ses_abc"},
        )
        assert dead.status_code == 409
        assert dead.json()["job_id"] == first.json()["job_id"]
        _alive(client)
        runner.release.set()
        job = _wait_job(client, first.json()["job_id"])
        assert job["status"] == "success"
        _dashboard(client, job["job_id"])
        again = client.post("/jobs", json=_ok(jira_id="LIVE-1"))
        assert again.status_code == 202
        _wait_job(client, again.json()["job_id"])
        _alive(client)


def test_queue_then_run_when_slot_frees(tmp_settings: Settings) -> None:
    tmp_settings.max_concurrent_jobs = 1
    runner = GateRunner()
    app = create_app(tmp_settings, runner=runner)
    with TestClient(app) as client:
        first = client.post("/jobs", json=_ok(jira_id="Q-1"))
        assert first.status_code == 202
        assert runner.entered.wait(timeout=5)
        second = client.post("/jobs", json=_ok(jira_id="Q-2"))
        assert second.status_code == 202
        queued = client.get("/api/queue").json()
        assert queued["queued_count"] >= 1
        poll_q = client.get(f"/jobs/{second.json()['job_id']}")
        assert poll_q.status_code == 202
        runner.release.set()
        assert _wait_job(client, first.json()["job_id"])["status"] == "success"
        assert _wait_job(client, second.json()["job_id"])["status"] == "success"
        assert client.get("/api/queue").json()["queued_count"] == 0
        _alive(client)


@pytest.mark.parametrize(
    "status,job_status,poll_code",
    [
        (200, "success", 200),
        (404, "not_found", 404),
        (500, "error", 500),
        (504, "timeout", 504),
    ],
)
def test_poller_terminal_codes(
    tmp_settings: Settings, status: int, job_status: str, poll_code: int
) -> None:
    app = create_app(tmp_settings, runner=StatusRunner(status, f"text-{status}"))
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id=f"TERM-{status}"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == job_status
        poll = client.get(f"/jobs/{res.json()['job_id']}")
        assert poll.status_code == 200
        assert poll.json()["status_code"] == poll_code
        assert poll.json()["live"] is False
        _dashboard(client, job["job_id"])
        _alive(client)


def test_missing_remote_branch_still_clones(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.clone_repo", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: type("R", (), {"status_code": 200, "text": "ok"})(),
    )
    dest = clone_path_for(tmp_settings.work_dir, "PROJ-12881")
    assert not dest.exists()
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="PROJ-12881", source_branch="no-such-branch"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "success"
        poll = client.get(f"/jobs/{res.json()['job_id']}")
        assert poll.status_code == 200
        assert poll.json()["status_code"] == 200
        _dashboard(client, job["job_id"])
        _alive(client)
        nxt = client.post("/jobs", json=_ok(jira_id="PROJ-12881"))
        assert nxt.status_code == 202
        _wait_job(client, nxt.json()["job_id"])
        _alive(client)


def test_leftover_clone_then_unused_branch_still_clones(tmp_settings: Settings, monkeypatch) -> None:
    dest = clone_path_for(tmp_settings.work_dir, "LEFT-1")
    dest.mkdir(parents=True)
    (dest / "README").write_text("old checkout", encoding="utf-8")
    seen: dict[str, bool] = {}

    def clone_ok(*_a, **_k) -> None:
        seen["existed"] = dest.exists()
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_ok)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: type("R", (), {"status_code": 200, "text": "ok"})(),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="LEFT-1", source_branch="ghost"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "success"
        assert seen["existed"] is False
        assert not dest.exists()
        _alive(client)


def test_leftover_clone_cannot_delete_is_error(tmp_settings: Settings, monkeypatch) -> None:
    dest = clone_path_for(tmp_settings.work_dir, "STUCK-1")
    dest.mkdir(parents=True)
    (dest / "x").write_text("held", encoding="utf-8")
    monkeypatch.setattr("opencode_manager.worker._remove_clone", lambda *_a, **_k: False)
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="STUCK-1"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "error"
        assert "leftover" in (job.get("error_message") or job.get("text") or "").lower()
        _alive(client)


def test_git_failures_are_errors_not_crashes(tmp_settings: Settings, monkeypatch) -> None:
    cases = [
        (
            "dns",
            lambda *_a, **_k: (_ for _ in ()).throw(
                GitError("git failed (128): Could not resolve host: gitlabent.company.com.tr")
            ),
            "error",
        ),
        (
            "timeout",
            lambda *_a, **_k: (_ for _ in ()).throw(GitError("git clone timed out after 1800s")),
            "error",
        ),
    ]
    for label, clone_fn, expect in cases:
        monkeypatch.setattr("opencode_manager.worker.clone_repo", clone_fn)
        app = create_app(tmp_settings)
        with TestClient(app) as client:
            res = client.post("/jobs", json=_ok(jira_id=f"GIT-{label}"))
            assert res.status_code == 202, label
            job = _wait_job(client, res.json()["job_id"])
            assert job["status"] == expect, (label, job)
            _dashboard(client, job["job_id"])
            _alive(client)


def test_opencode_failures_are_errors_not_crashes(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.clone_repo", lambda *_a, **_k: None)

    def fail_model(*_a, **_k):
        raise JobFailed(500, "unknown model 'nope/x'; available: ollama/Restricted-Kimi-K2.6")

    def fail_timeout(*_a, **_k):
        raise JobFailed(504, "attempt timed out")

    def fail_boom(*_a, **_k):
        raise RuntimeError("serve exploded")

    for jira, fn, expect in (
        ("OC-MODEL", fail_model, "error"),
        ("OC-TIME", fail_timeout, "timeout"),
        ("OC-BOOM", fail_boom, "error"),
    ):
        monkeypatch.setattr("opencode_manager.worker.run_opencode_job", fn)
        dest = clone_path_for(tmp_settings.work_dir, jira)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "keep").write_text("clone", encoding="utf-8")
        app = create_app(tmp_settings)
        with TestClient(app) as client:
            res = client.post("/jobs", json=_ok(jira_id=jira))
            assert res.status_code == 202, jira
            job = _wait_job(client, res.json()["job_id"])
            assert job["status"] == expect, (jira, job)
            assert not dest.exists(), jira
            _dashboard(client, job["job_id"])
            _alive(client)


def test_runner_exception_then_next_job(tmp_settings: Settings) -> None:
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


def test_cleanup_explosions_on_missing_branch(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(GitError("clone exploded")),
    )
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
    monkeypatch.setattr(
        "opencode_manager.cleanup.kill._windows_cwd",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("peb")),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        res = client.post("/jobs", json=_ok(jira_id="HOLD-1", source_branch="missing"))
        assert res.status_code == 202
        job = _wait_job(client, res.json()["job_id"])
        assert job["status"] == "error"
        _alive(client)


def test_mixed_burst_good_and_bad(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.worker.clone_repo", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "opencode_manager.worker.run_opencode_job",
        lambda *_a, **_k: type("R", (), {"status_code": 200, "text": "ok"})(),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        accepted: List[str] = []
        for i in range(6):
            bad = client.post("/jobs", json=_ok(jira_id=f"BURST/{i}", source_branch="-1"))
            assert bad.status_code == 400
            miss = client.post("/jobs", json=_ok(jira_id=f"MISS-{i}", source_branch="nope"))
            assert miss.status_code == 202
            accepted.append(miss.json()["job_id"])
            _alive(client)
        for job_id in accepted:
            job = _wait_job(client, job_id)
            assert job["status"] == "success"
            _dashboard(client, job_id)
        good_app = create_app(tmp_settings, runner=FakeRunner())
        with TestClient(good_app) as good_client:
            for i in range(6):
                good = good_client.post("/jobs", json=_ok(jira_id=f"BURST-{i}", agent_mode="planner"))
                assert good.status_code == 202
                job = _wait_job(good_client, good.json()["job_id"])
                assert job["status"] == "success"
                _alive(good_client)


def test_session_delete_bad_and_good_keep_app_up(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        for body in (
            {},
            {"jira_id": "PROJ-1"},
            {"session_id": "ses_abc"},
            {"jira_id": "PROJ-1", "session_id": "-1"},
            {"jira_id": "PROJ-1", "session_id": "  -1  "},
            {"jira_id": "PROJ-1", "session_id": "not-ses"},
            {"jira_id": "../x", "session_id": "ses_abc"},
            {"jira_id": "", "session_id": "ses_abc"},
            {"jira_id": "PROJ/1", "session_id": "ses_abc"},
            {"jira_id": ".", "session_id": "ses_abc"},
            {"jira_id": "A" + "x" * 80, "session_id": "ses_abc"},
        ):
            res = client.request("DELETE", "/sessions", json=body)
            assert res.status_code in {400, 503}, body
            assert res.json()["job_id"] == ""
            _alive(client)


def test_callback_host_block_is_400_not_crash(tmp_settings: Settings) -> None:
    tmp_settings.callback_allowed_hosts = ["n8n.example.com"]
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        blocked = client.post(
            "/jobs",
            json=_ok(jira_id="CB-BAD", callback_url="https://evil.example/wait"),
        )
        assert blocked.status_code == 400
        assert "not allowed" in blocked.json()["text"].lower()
        ok = client.post(
            "/jobs",
            json=_ok(jira_id="CB-OK", callback_url="https://n8n.example.com/wait"),
        )
        assert ok.status_code == 202
        _wait_job(client, ok.json()["job_id"])
        _alive(client)


def test_filters_after_mixed_outcomes(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, runner=FakeRunner())
    with TestClient(app) as client:
        ok = client.post("/jobs", json=_ok(jira_id="FILT-OK"))
        assert ok.status_code == 202
        _wait_job(client, ok.json()["job_id"])
        from opencode_manager.dashboard.store import JobStore

        store = JobStore(tmp_settings.job_store_dir)
        store.save(
            JobRecord(
                job_id="job_seed_err",
                jira_id="FILT-ERR",
                status="error",
                live=False,
                error_message="seeded",
            )
        )
        all_jobs = client.get("/api/jobs?filter=all").json()
        completed = client.get("/api/jobs?filter=completed").json()
        errors = client.get("/api/jobs?filter=error").json()
        active = client.get("/api/jobs?filter=active").json()
        assert all_jobs["total"] >= 2
        assert any(j["job_id"] == ok.json()["job_id"] for j in completed["jobs"])
        assert any(j["job_id"] == "job_seed_err" for j in errors["jobs"])
        assert active["total"] == 0
        _alive(client)


def test_same_ticket_after_error_is_new_job(tmp_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "opencode_manager.worker.clone_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(GitError("git failed (128): Could not resolve host: x")),
    )
    app = create_app(tmp_settings)
    with TestClient(app) as client:
        first = client.post("/jobs", json=_ok(jira_id="AGAIN-1"))
        assert first.status_code == 202
        job1 = _wait_job(client, first.json()["job_id"])
        assert job1["status"] == "error"
        second = client.post("/jobs", json=_ok(jira_id="AGAIN-1"))
        assert second.status_code == 202
        assert second.json()["job_id"] != first.json()["job_id"]
        job2 = _wait_job(client, second.json()["job_id"])
        assert job2["status"] == "error"
        _alive(client)
