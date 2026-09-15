"""Break the live HTTP server with real TCP requests.

uvicorn + httpx (and a few raw sockets). FakeRunner for volume so we
are testing the HTTP surface, not OpenCode. One test uses the real
worker so a failed clone cannot take the process down.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import httpx
import pytest
import uvicorn

from opencode_manager.app import create_app
from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal
from tests.test_api import FakeRunner
from tests.test_crash_inputs import GateRunner


def _ok(**overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "do work",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 30,
        "retry_count": 1,
        "jira_id": "HTTP-1",
        "callback_url": "",
    }
    data.update(overrides)
    return data


_USE_FAKE = object()


class LiveHttp:
    def __init__(self, settings: Settings, runner: object = _USE_FAKE) -> None:
        self.settings = settings
        self.app = create_app(
            settings,
            runner=FakeRunner() if runner is _USE_FAKE else runner,
        )
        self.config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="error",
            lifespan="on",
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="osm-http-break", daemon=True)
        self.thread.start()
        deadline = time.time() + 8
        while time.time() < deadline and not self.server.started:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("uvicorn did not start")
        sock = self.server.servers[0].sockets[0]
        self.port = int(sock.getsockname()[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.client = httpx.Client(base_url=self.base, timeout=10.0)

    def close(self) -> None:
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=12)


def _seed_spa(settings: Settings) -> None:
    dist = settings.project_root / "web" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html>SPA-INDEX</html>", encoding="utf-8")


@pytest.fixture
def live(tmp_settings: Settings) -> LiveHttp:
    tmp_settings.gitlab_webhook_secret = "tank"
    tmp_settings.azure_webhook_user = "hook"
    tmp_settings.azure_webhook_password = "secret"
    _seed_spa(tmp_settings)
    http = LiveHttp(tmp_settings)
    try:
        yield http
    finally:
        http.close()


def _wait_poll(client: httpx.Client, job_id: str, *, timeout: float = 8.0) -> httpx.Response:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/jobs/{job_id}")
        if last.status_code == 200 and last.json().get("live") is False:
            return last
        time.sleep(0.04)
    raise AssertionError(f"job {job_id} still live: {last.status_code if last else None} {last.text if last else ''}")


def _alive(client: httpx.Client) -> None:
    meta = client.get("/api/meta")
    assert meta.status_code == 200, meta.text
    assert "version" in meta.json()
    jobs = client.get("/api/jobs?page=1&page_size=25")
    assert jobs.status_code == 200, jobs.text
    assert "jobs" in jobs.json()


# ---------------------------------------------------------------------------
# Good / bad POST /jobs over real TCP
# ---------------------------------------------------------------------------

BAD_JOBS: List[tuple[str, Any, int]] = [
    ("empty object", {}, 400),
    ("list body", [], 400),
    ("missing prompt", {k: v for k, v in _ok().items() if k != "prompt"}, 400),
    ("null prompt", _ok(prompt=None), 400),
    ("blank prompt", _ok(prompt="   "), 400),
    ("ssh git@", _ok(repo_url="git@host:g/r.git"), 400),
    ("ssh scheme", _ok(repo_url="ssh://git@host/g/r.git"), 400),
    ("ftp repo", _ok(repo_url="ftp://host/r.git"), 400),
    ("blank repo", _ok(repo_url="  "), 400),
    ("model no slash", _ok(model="nopath"), 400),
    ("model slash only", _ok(model="/"), 400),
    ("agent wizard", _ok(agent_mode="wizard"), 400),
    ("agent plan", _ok(agent_mode="plan"), 400),
    ("agent build", _ok(agent_mode="build"), 400),
    ("working_mode only", {k: v for k, v in _ok(working_mode="Plan").items() if k != "agent_mode"}, 400),
    ("jira slash", _ok(jira_id="PROJ/99"), 400),
    ("jira backslash", _ok(jira_id="PROJ\\99"), 400),
    ("jira dot", _ok(jira_id="."), 400),
    ("jira dotdot", _ok(jira_id=".."), 400),
    ("jira space", _ok(jira_id="a b"), 400),
    ("jira unicode", _ok(jira_id="PROJ-üğ"), 400),
    ("jira leading hyphen", _ok(jira_id="-KAN-1"), 400),
    ("jira empty", _ok(jira_id=""), 400),
    ("jira too long", _ok(jira_id="A" + "x" * 80), 400),
    ("timeout zero", _ok(timeout_in_seconds=0), 400),
    ("timeout negative", _ok(timeout_in_seconds=-5), 400),
    ("timeout text", _ok(timeout_in_seconds="nope"), 400),
    ("retry text", _ok(retry_count="nope"), 400),
    ("callback relative", _ok(callback_url="not-a-url"), 400),
    ("callback ftp", _ok(callback_url="ftp://x/y"), 400),
    ("callback no host", _ok(callback_url="http://"), 400),
    ("callback ws", _ok(callback_url="ws://127.0.0.1/x"), 400),
    ("missing timeout", {k: v for k, v in _ok().items() if k != "timeout_in_seconds"}, 400),
    ("missing retry", {k: v for k, v in _ok().items() if k != "retry_count"}, 400),
    ("missing jira", {k: v for k, v in _ok().items() if k != "jira_id"}, 400),
    ("missing model", {k: v for k, v in _ok().items() if k != "model"}, 400),
    ("missing repo", {k: v for k, v in _ok().items() if k != "repo_url"}, 400),
]


@pytest.mark.parametrize("label,body,expect", BAD_JOBS, ids=[row[0] for row in BAD_JOBS])
def test_real_http_bad_post_jobs(live: LiveHttp, label: str, body: Any, expect: int) -> None:
    res = live.client.post("/jobs", json=body)
    assert res.status_code == expect, f"{label}: {res.status_code} {res.text}"
    payload = res.json()
    assert payload.get("status_code") == expect
    assert payload.get("job_id") == ""
    _alive(live.client)


GOOD_JOBS: List[tuple[str, Dict[str, Any]]] = [
    ("orchestrator", _ok(jira_id="GOOD-ORCH")),
    ("planner", _ok(jira_id="GOOD-PLAN", agent_mode="planner")),
    ("session -1", _ok(jira_id="GOOD-SES1", session_id="-1")),
    ("session empty", _ok(jira_id="GOOD-SESE", session_id="")),
    ("session uuid", _ok(jira_id="GOOD-UUID", session_id="550e8400-e29b-41d4-a716-446655440000")),
    ("unicode prompt", _ok(jira_id="GOOD-UNI", prompt="Türkçe plan: geliştir")),
    ("long prompt", _ok(jira_id="GOOD-LONG", prompt="x" * 8000)),
    ("branch -1", _ok(jira_id="GOOD-BR1", source_branch="-1")),
    ("branch empty", _ok(jira_id="GOOD-BRE", source_branch="")),
    ("no branch", {k: v for k, v in _ok(jira_id="GOOD-NOBR").items() if k != "source_branch"}),
    ("retry zero", _ok(jira_id="GOOD-R0", retry_count=0)),
    ("timeout string", _ok(jira_id="GOOD-TS", timeout_in_seconds="45")),
    ("file repo", _ok(jira_id="GOOD-FILE", repo_url="file:///tmp/repo.git")),
    ("https callback", _ok(jira_id="GOOD-CB", callback_url="https://n8n.example/wait/abc")),
    ("extra PAT ignored", _ok(jira_id="GOOD-XTRA", PAT="secret-token")),
    ("dotted jira", _ok(jira_id="PROJ.1_2-3")),
    ("model extra slash", _ok(jira_id="GOOD-MDL", model="ollama/org/Restricted-Kimi-K2.6")),
]


@pytest.mark.parametrize("label,body", GOOD_JOBS, ids=[row[0] for row in GOOD_JOBS])
def test_real_http_good_post_jobs(live: LiveHttp, label: str, body: Dict[str, Any]) -> None:
    res = live.client.post("/jobs", json=body)
    assert res.status_code == 202, f"{label}: {res.status_code} {res.text}"
    env = res.json()
    assert env["status_code"] == 202
    assert env["job_id"].startswith("job_")
    assert env["jira_id"] == body["jira_id"]
    done = _wait_poll(live.client, env["job_id"])
    assert done.json()["live"] is False
    assert done.json()["status_code"] in {200, 500, 504}
    _alive(live.client)


def test_real_http_poller_is_json_not_spa(live: LiveHttp) -> None:
    missing = live.client.get("/jobs/job_doesnotexist")
    assert missing.status_code == 404
    assert missing.headers.get("content-type", "").startswith("application/json")
    assert "status_code" in missing.json()
    spa = live.client.get("/no-such-page")
    assert spa.status_code == 200
    assert "SPA-INDEX" in spa.text
    _alive(live.client)


def test_real_http_same_ticket_concurrent_one_202(tmp_settings: Settings) -> None:
    tmp_settings.max_concurrent_jobs = 1
    gate = GateRunner()
    http = LiveHttp(tmp_settings, runner=gate)
    try:
        results: List[httpx.Response] = []

        def fire() -> None:
            results.append(http.client.post("/jobs", json=_ok(jira_id="RACE-1")))

        threads = [threading.Thread(target=fire) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        codes = [r.status_code for r in results]
        assert codes.count(202) == 1, codes
        assert codes.count(409) == 15, codes
        winners = [r.json()["job_id"] for r in results if r.status_code == 202]
        losers = [r.json()["job_id"] for r in results if r.status_code == 409]
        assert winners[0]
        assert all(job_id == winners[0] for job_id in losers)
        gate.release.set()
        _wait_poll(http.client, winners[0])
        _alive(http.client)
    finally:
        gate.release.set()
        http.close()


def test_real_http_many_tickets_all_accepted(live: LiveHttp) -> None:
    accepted: List[str] = []
    for i in range(24):
        res = live.client.post("/jobs", json=_ok(jira_id=f"BATCH-{i}"))
        assert res.status_code == 202, res.text
        accepted.append(res.json()["job_id"])
    for job_id in accepted:
        done = _wait_poll(live.client, job_id)
        assert done.json()["status_code"] == 200
    listing = live.client.get("/api/jobs?page=1&page_size=25")
    assert listing.json()["total"] >= 24
    _alive(live.client)


def test_real_http_mixed_storm_process_stays_up(live: LiveHttp) -> None:
    errors: List[str] = []

    def worker(i: int) -> None:
        try:
            if i % 5 == 0:
                r = live.client.post("/jobs", json=_ok(jira_id=f"STORM-{i}"))
                assert r.status_code == 202
            elif i % 5 == 1:
                r = live.client.post("/jobs", json=_ok(jira_id="PROJ/bad"))
                assert r.status_code == 400
            elif i % 5 == 2:
                r = live.client.get("/jobs/job_missing")
                assert r.status_code == 404
            elif i % 5 == 3:
                r = live.client.request("DELETE", "/sessions", json={"jira_id": "X-1", "session_id": "-1"})
                assert r.status_code == 400
            else:
                r = live.client.get("/api/jobs?filter=error")
                assert r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{i}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert errors == []
    _alive(live.client)


# ---------------------------------------------------------------------------
# Auth, dashboard writes, webhooks
# ---------------------------------------------------------------------------


def test_real_http_auth_blocks_n8n_and_dashboard(tmp_settings: Settings) -> None:
    tmp_settings.dashboard_user = "admin"
    tmp_settings.dashboard_password = "admin"
    tmp_settings.dashboard_token = "change_me"
    http = LiveHttp(tmp_settings)
    try:
        assert http.client.get("/api/jobs").status_code == 401
        assert http.client.post("/jobs", json=_ok(jira_id="AUTH-1")).status_code == 401
        assert http.client.get("/jobs/job_x").status_code == 401
        bad = http.client.post("/jobs", json=_ok(jira_id="AUTH-1"), headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        ok = http.client.post(
            "/jobs",
            json=_ok(jira_id="AUTH-1"),
            headers={"Authorization": "Bearer change_me"},
        )
        assert ok.status_code == 202, ok.text
        login = http.client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        cookie = login.cookies.get("amir_mini_session")
        assert cookie
        authed = httpx.Client(base_url=http.base, timeout=10.0, cookies={"amir_mini_session": cookie})
        try:
            assert authed.get("/api/jobs").status_code == 200
            assert authed.get("/api/meta").status_code == 200
        finally:
            authed.close()
        _alive(httpx.Client(base_url=http.base, timeout=10.0, headers={"Authorization": "Bearer change_me"}))
    finally:
        http.close()


def test_real_http_dashboard_writes_are_405(live: LiveHttp) -> None:
    for method, path in (
        ("POST", "/api/jobs"),
        ("PATCH", "/api/jobs/x"),
        ("PUT", "/api/jobs/x"),
        ("DELETE", "/api/jobs/x"),
        ("POST", "/api/report-context"),
        ("DELETE", "/api/queue"),
    ):
        res = live.client.request(method, path, json={})
        assert res.status_code == 405, f"{method} {path} -> {res.status_code} {res.text}"
    _alive(live.client)


def test_real_http_gitlab_webhook_secret_and_garbage(live: LiveHttp) -> None:
    no_token = live.client.post("/amirmini/webhook/gitlab", json={"object_kind": "note"})
    assert no_token.status_code == 401
    wrong = live.client.post(
        "/amirmini/webhook/gitlab",
        json={"object_kind": "note"},
        headers={"X-Gitlab-Token": "wrong"},
    )
    assert wrong.status_code == 401
    bad_json = live.client.post(
        "/amirmini/webhook/gitlab",
        content=b"not-json",
        headers={"X-Gitlab-Token": "tank", "Content-Type": "application/json"},
    )
    assert bad_json.status_code == 400
    ignored = live.client.post(
        "/amirmini/webhook/gitlab",
        json={"object_kind": "push", "project": {"id": 1}},
        headers={"X-Gitlab-Token": "tank"},
    )
    assert ignored.status_code == 200
    assert ignored.json().get("status") in {"ignored", "accepted"}
    _alive(live.client)


def test_real_http_azure_webhook_basic(live: LiveHttp) -> None:
    bare = live.client.post("/amirmini/webhook/azure", json={"eventType": "git.pullrequest.created"})
    assert bare.status_code == 401
    import base64

    token = base64.b64encode(b"hook:secret").decode("ascii")
    res = live.client.post(
        "/amirmini/webhook/azure",
        json={"eventType": "git.pullrequest.updated", "resource": {}},
        headers={"Authorization": f"Basic {token}"},
    )
    assert res.status_code in {200, 400}
    _alive(live.client)


def test_real_http_delete_sessions_contract(live: LiveHttp) -> None:
    cases = [
        ({}, 400),
        ({"jira_id": "X-1"}, 400),
        ({"session_id": "ses_abc"}, 400),
        ({"jira_id": "X-1", "session_id": "-1"}, 400),
        ({"jira_id": "X-1", "session_id": "not-ses"}, 400),
        ({"jira_id": "../x", "session_id": "ses_abc"}, 400),
        ({"jira_id": "X-1", "session_id": "550e8400-e29b-41d4-a716-446655440000"}, 400),
    ]
    for body, expect in cases:
        res = live.client.request("DELETE", "/sessions", json=body)
        assert res.status_code == expect, f"{body} -> {res.status_code} {res.text}"
        assert res.json()["status_code"] == expect
        assert res.json()["job_id"] == ""
    _alive(live.client)


def test_real_http_delete_sessions_409_while_job_live(tmp_settings: Settings) -> None:
    gate = GateRunner()
    http = LiveHttp(tmp_settings, runner=gate)
    try:
        accepted = http.client.post("/jobs", json=_ok(jira_id="DEL-LIVE"))
        assert accepted.status_code == 202
        assert gate.entered.wait(timeout=5)
        res = http.client.request(
            "DELETE",
            "/sessions",
            json={"jira_id": "DEL-LIVE", "session_id": "ses_abc"},
        )
        assert res.status_code == 409, res.text
        assert res.json()["job_id"] == accepted.json()["job_id"]
        gate.release.set()
        _wait_poll(http.client, accepted.json()["job_id"])
    finally:
        gate.release.set()
        http.close()


def test_real_http_callback_host_allowlist(tmp_settings: Settings) -> None:
    tmp_settings.callback_allowed_hosts = ["n8n.example.com"]
    http = LiveHttp(tmp_settings)
    try:
        denied = http.client.post(
            "/jobs",
            json=_ok(jira_id="CB-DENY", callback_url="http://127.0.0.1:9/wait"),
        )
        assert denied.status_code == 400, denied.text
        assert "callback" in denied.json()["text"].lower()
        allowed = http.client.post(
            "/jobs",
            json=_ok(jira_id="CB-OK", callback_url="https://n8n.example.com/wait/1"),
        )
        assert allowed.status_code == 202, allowed.text
        _wait_poll(http.client, allowed.json()["job_id"])
    finally:
        http.close()


def test_real_http_callback_is_posted_once(tmp_settings: Settings) -> None:
    hits: List[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            with lock:
                hits.append(json.loads(raw.decode("utf-8")))
            self.send_response(200)
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    http = LiveHttp(tmp_settings)
    try:
        res = http.client.post(
            "/jobs",
            json=_ok(jira_id="CB-ONCE", callback_url=f"http://{host}:{port}/wait"),
        )
        assert res.status_code == 202
        _wait_poll(http.client, res.json()["job_id"])
        deadline = time.time() + 4
        while time.time() < deadline and not hits:
            time.sleep(0.05)
        assert len(hits) == 1, hits
        assert hits[0]["job_id"] == res.json()["job_id"]
        assert hits[0]["status_code"] == 200
        assert hits[0]["jira_id"] == "CB-ONCE"
    finally:
        http.close()
        httpd.shutdown()


# ---------------------------------------------------------------------------
# Raw sockets / traversal / huge body / real worker
# ---------------------------------------------------------------------------


def test_real_http_path_traversal_stays_json_or_spa(live: LiveHttp) -> None:
    for path in (
        "/jobs/../../settings.yaml",
        "/api/jobs/../../settings.yaml",
        "/jobs/%2e%2e/%2e%2e/settings.yaml",
        "/assets/../../settings.yaml",
    ):
        res = live.client.get(path)
        assert res.status_code in {200, 404}, f"{path} -> {res.status_code}"
        assert "dashboard_password" not in res.text
        assert "gitlab_token" not in res.text
    _alive(live.client)


def test_real_http_invalid_json_and_wrong_content_type(live: LiveHttp) -> None:
    raw = live.client.post(
        "/jobs",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert raw.status_code in {400, 422}
    plain = live.client.post(
        "/jobs",
        content=b"prompt=hi",
        headers={"Content-Type": "text/plain"},
    )
    assert plain.status_code in {400, 415, 422}
    empty = live.client.post("/jobs", content=b"", headers={"Content-Type": "application/json"})
    assert empty.status_code in {400, 422}
    _alive(live.client)


def test_real_http_huge_prompt_is_accepted_or_rejected_cleanly(live: LiveHttp) -> None:
    res = live.client.post("/jobs", json=_ok(jira_id="HUGE-P", prompt="x" * 200_000))
    assert res.status_code in {202, 400, 413}, res.status_code
    if res.status_code == 202:
        _wait_poll(live.client, res.json()["job_id"])
    _alive(live.client)


def test_real_http_raw_socket_garbage_does_not_kill_server(live: LiveHttp) -> None:
    payloads = [
        b"GET /api/meta HTTP/1.0\r\n\r\n",
        b"POST /jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 3\r\n\r\nno",
        b"\x00\x01\x02\x03",
        b"GET /jobs/%00 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        b"TRACE /jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
    ]
    for blob in payloads:
        sock = socket.create_connection(("127.0.0.1", live.port), timeout=3)
        try:
            sock.sendall(blob)
            sock.settimeout(2)
            try:
                sock.recv(1024)
            except (TimeoutError, ConnectionError, OSError):
                pass
        finally:
            sock.close()
    _alive(live.client)


def test_real_http_filters_and_unknown_job_detail(live: LiveHttp) -> None:
    res = live.client.post("/jobs", json=_ok(jira_id="FILT-1"))
    job_id = res.json()["job_id"]
    _wait_poll(live.client, job_id)
    for filt in ("all", "active", "error", "completed", "review", "not-a-filter"):
        listing = live.client.get("/api/jobs", params={"filter": filt, "page": 1, "page_size": 25})
        assert listing.status_code == 200, filt
        assert "jobs" in listing.json()
    missing = live.client.get("/api/jobs/job_nope")
    assert missing.status_code == 404
    chat = live.client.get("/api/jobs/job_nope/chat")
    assert chat.status_code == 404
    _alive(live.client)


def test_real_http_userinfo_not_in_public_or_logs(live: LiveHttp) -> None:
    dirty = "https://oauth2:super-secret-pat@gitlab.example/g/r.git"
    res = live.client.post("/jobs", json=_ok(jira_id="SCRUB-1", repo_url=dirty, session_id="-1"))
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    _wait_poll(live.client, job_id)
    detail = live.client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    blob = detail.text
    assert "super-secret-pat" not in blob
    assert "oauth2:" not in (detail.json()["job"].get("repo_url") or "")
    assert "callback_url" not in detail.json()["job"]
    _alive(live.client)


def test_real_http_worker_clone_fail_does_not_kill_server(tmp_settings: Settings) -> None:
    """No FakeRunner: real OpenCodeRunner, clone to a closed port, process stays up."""
    tmp_settings.git_clone_timeout_seconds = 8.0
    http = LiveHttp(tmp_settings, runner=None)
    try:
        res = http.client.post(
            "/jobs",
            json=_ok(
                jira_id="REALFAIL-1",
                repo_url="https://127.0.0.1:1/nope.git",
                timeout_in_seconds=15,
            ),
        )
        assert res.status_code == 202, res.text
        done = _wait_poll(http.client, res.json()["job_id"], timeout=20)
        assert done.json()["live"] is False
        assert done.json()["status_code"] in {500, 504}
        _alive(http.client)
        again = http.client.post(
            "/jobs",
            json=_ok(jira_id="REALFAIL-1", repo_url="https://127.0.0.1:1/nope.git"),
        )
        assert again.status_code == 202, again.text
        _wait_poll(http.client, again.json()["job_id"], timeout=20)
        _alive(http.client)
    finally:
        http.close()
