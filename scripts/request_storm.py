"""Start OSM and fire 100+ mixed HTTP requests. Process must stay up."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opencode_manager.app import create_app  # noqa: E402
from opencode_manager.models import JobRecord  # noqa: E402
from opencode_manager.settings import Settings  # noqa: E402
from opencode_manager.worker import Terminal  # noqa: E402


class FastRunner:
    def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
        job.session_id = job.session_id or "ses_storm"
        job.text = f"ok {job.jira_id}"
        return Terminal(200, job.text)


def _ok(**overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "STORM-1",
        "callback_url": "",
    }
    data.update(overrides)
    return data


def build_cases() -> List[Tuple[str, str, str, Any]]:
    """(label, method, path, json_or_none)."""
    cases: List[Tuple[str, str, str, Any]] = []

    # Valid GETs
    for path in (
        "/api/meta",
        "/api/jobs",
        "/api/jobs?page=1&page_size=25",
        "/api/jobs?filter=all",
        "/api/jobs?filter=active",
        "/api/jobs?filter=error",
        "/api/jobs?filter=completed",
        "/api/jobs?jira_id=STORM-1",
        "/api/queue",
        "/api/queue?jira_id=STORM-1",
        "/api/report-context",
        "/jobs",
        "/",
    ):
        cases.append((f"GET {path}", "GET", path, None))

    # Invalid / edge GETs
    for path in (
        "/jobs/nope",
        "/jobs/job_missing",
        "/jobs/%00",
        "/api/jobs/nope",
        "/api/jobs/nope/chat",
        "/api/jobs/nope/prompts",
        "/api/jobs/nope/logs",
        "/api/jobs/nope/serve-log",
        "/api/jobs/job_-1/chat",
        "/api/jobs?page=-1",
        "/api/jobs?page=0",
        "/api/jobs?page=9999",
        "/api/jobs?page_size=0",
        "/api/jobs?page_size=101",
        "/api/jobs?filter=not-a-filter",
        "/api/jobs?filter=error&jira_id=../x",
        "/api/queue?jira_id=PROJ/99",
        "/no-such-route",
        "/api/jobs/../jobs",
    ):
        cases.append((f"GET {path}", "GET", path, None))

    # Dashboard writes must stay GET-only
    for method, path in (
        ("POST", "/api/report-context"),
        ("POST", "/api/jobs"),
        ("PATCH", "/api/jobs/x"),
        ("PUT", "/api/jobs/x"),
        ("DELETE", "/api/jobs/x"),
        ("PATCH", "/jobs"),
    ):
        cases.append((f"{method} {path}", method, path, {}))

    bad_jobs: List[Tuple[str, Any]] = [
        ("empty object", {}),
        ("null prompt", _ok(prompt=None)),
        ("blank prompt", _ok(prompt="   ")),
        ("missing prompt", {k: v for k, v in _ok().items() if k != "prompt"}),
        ("ssh git@", _ok(repo_url="git@host:g/r.git")),
        ("ssh scheme", _ok(repo_url="ssh://git@host/g/r.git")),
        ("ftp repo", _ok(repo_url="ftp://host/r.git")),
        ("blank repo", _ok(repo_url="  ")),
        ("model no slash", _ok(model="nopath")),
        ("model slash only", _ok(model="/")),
        ("model provider only", _ok(model="ollama/")),
        ("agent plan", _ok(agent_mode="plan")),
        ("agent build", _ok(agent_mode="build")),
        ("agent wizard", _ok(agent_mode="wizard")),
        ("agent blank", _ok(agent_mode="  ")),
        ("only working_mode", {k: v for k, v in _ok(working_mode="Plan").items() if k != "agent_mode"}),
        ("jira slash", _ok(jira_id="PROJ/99")),
        ("jira backslash", _ok(jira_id="PROJ\\99")),
        ("jira dot", _ok(jira_id=".")),
        ("jira dotdot", _ok(jira_id="..")),
        ("jira space", _ok(jira_id="a b")),
        ("jira unicode", _ok(jira_id="PROJ-üğ")),
        ("jira leading hyphen", _ok(jira_id="-KAN-1")),
        ("jira empty", _ok(jira_id="")),
        ("jira too long", _ok(jira_id="A" + "x" * 80)),
        ("timeout zero", _ok(timeout_in_seconds=0)),
        ("timeout negative", _ok(timeout_in_seconds=-5)),
        ("timeout text", _ok(timeout_in_seconds="nope")),
        ("retry text", _ok(retry_count="nope")),
        ("callback relative", _ok(callback_url="not-a-url")),
        ("callback ftp", _ok(callback_url="ftp://x/y")),
        ("callback no host", _ok(callback_url="http://")),
        ("callback ws", _ok(callback_url="ws://127.0.0.1/x")),
        ("missing timeout", {k: v for k, v in _ok().items() if k != "timeout_in_seconds"}),
        ("missing retry", {k: v for k, v in _ok().items() if k != "retry_count"}),
        ("missing jira", {k: v for k, v in _ok().items() if k != "jira_id"}),
        ("missing model", {k: v for k, v in _ok().items() if k != "model"}),
        ("missing repo", {k: v for k, v in _ok().items() if k != "repo_url"}),
    ]
    for i in range(10):
        bad_jobs.append((f"jira slash variant {i}", _ok(jira_id=f"PROJ/{i}")))
    for label, body in bad_jobs:
        cases.append((f"POST bad {label}", "POST", "/jobs", body))

    good_jobs: List[Tuple[str, Dict[str, Any]]] = [
        ("orchestrator", _ok(jira_id="GOOD-ORCH")),
        ("planner", _ok(jira_id="GOOD-PLAN", agent_mode="planner")),
        ("agent_type", {**{k: v for k, v in _ok(jira_id="GOOD-TYPE").items() if k != "agent_mode"}, "agent_type": "planner"}),
        ("session -1", _ok(jira_id="GOOD-SES1", session_id="-1")),
        ("session empty", _ok(jira_id="GOOD-SESE", session_id="")),
        ("session uuid", _ok(jira_id="GOOD-UUID", session_id="550e8400-e29b-41d4-a716-446655440000")),
        ("session ses_", _ok(jira_id="GOOD-SES", session_id="ses_abc123")),
        ("unicode prompt", _ok(jira_id="GOOD-UNI", prompt="Türkçe plan: geliştir")),
        ("long prompt", _ok(jira_id="GOOD-LONG", prompt="x" * 8000)),
        ("feature branch", _ok(jira_id="GOOD-FEAT", source_branch="feature/KAN-9")),
        ("missing branch", {k: v for k, v in _ok(jira_id="GOOD-NOBR").items() if k != "source_branch"}),
        ("branch -1", _ok(jira_id="GOOD-BR1", source_branch="-1")),
        ("branch empty", _ok(jira_id="GOOD-BRE", source_branch="")),
        ("retry zero", _ok(jira_id="GOOD-R0", retry_count=0)),
        ("timeout string", _ok(jira_id="GOOD-TS", timeout_in_seconds="45")),
        ("file repo", _ok(jira_id="GOOD-FILE", repo_url="file:///tmp/repo.git")),
        ("https callback", _ok(jira_id="GOOD-CB", callback_url="https://n8n.example/wait/abc")),
        ("extra fields", _ok(jira_id="GOOD-XTRA", working_mode="Plan", PAT="secret-token")),
        ("dotted jira", _ok(jira_id="PROJ.1_2-3")),
        ("model extra slash", _ok(jira_id="GOOD-MDL", model="ollama/org/Restricted-Kimi-K2.6")),
    ]
    for i in range(20):
        good_jobs.append((f"batch {i}", _ok(jira_id=f"BATCH-{i}")))
    for label, body in good_jobs:
        cases.append((f"POST good {label}", "POST", "/jobs", body))

    # Duplicate ticket while first may still be finishing → 409 or 202
    cases.append(("POST dup GOOD-ORCH", "POST", "/jobs", _ok(jira_id="GOOD-ORCH")))

    bad_deletes = [
        {},
        {"jira_id": "X-1"},
        {"session_id": "ses_abc"},
        {"jira_id": "", "session_id": "ses_abc"},
        {"jira_id": "X-1", "session_id": ""},
        {"jira_id": "X-1", "session_id": "-1"},
        {"jira_id": "X-1", "session_id": "not-ses"},
        {"jira_id": "../x", "session_id": "ses_abc"},
        {"jira_id": "X-1", "session_id": "550e8400-e29b-41d4-a716-446655440000"},
    ]
    for i, body in enumerate(bad_deletes):
        cases.append((f"DELETE sessions {i}", "DELETE", "/sessions", body))

    return cases


def main() -> int:
    data = Path.cwd() / "logs" / "storm-data"
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=4097,
        max_concurrent_jobs=4,
        callback_timeout_seconds=2.0,
        callback_retry_count=1,
        work_dir=data / "work",
        job_log_dir=data / "joblogs",
        job_store_dir=data / "jobs",
        queue_path=data / "queue.json",
        log_level="WARNING",
        hang_timeout_seconds=30.0,
        git_clone_timeout_seconds=10.0,
        project_root=ROOT,
    )
    settings.ensure_dirs()
    app = create_app(settings, runner=FastRunner())
    config = uvicorn.Config(app, host="127.0.0.1", port=4097, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="osm-storm", daemon=True)
    thread.start()
    base = "http://127.0.0.1:4097"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/meta", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        print("server did not become ready")
        return 1

    cases = build_cases()
    print(f"firing {len(cases)} requests")
    results: List[str] = []
    ok = 0
    with httpx.Client(base_url=base, timeout=15.0) as client:
        for label, method, path, body in cases:
            try:
                res = client.request(method, path, json=body)
                code = res.status_code
                results.append(f"{code:3d} {label}")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                results.append(f"ERR {label} {type(exc).__name__}: {exc}")
        # After storm, app must still answer
        try:
            meta = client.get("/api/meta")
            listing = client.get("/api/jobs?page=1&page_size=25")
            queue = client.get("/api/queue")
            alive = (
                meta.status_code == 200
                and listing.status_code == 200
                and "jobs" in listing.json()
                and queue.status_code == 200
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ALIVE CHECK FAILED: {exc}")
            alive = False

    for line in results:
        print(line)
    print(f"sent={len(cases)} completed={ok} alive={alive}")
    if not alive or ok < 100:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
