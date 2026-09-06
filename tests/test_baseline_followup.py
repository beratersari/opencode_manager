"""Follow-up jobs must not ship the previous turn when baseline list fails.

Integration coverage for `_load_turn_baseline` through `run_opencode_job`
and `POST /jobs` + poller + callback.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from json import loads
from typing import List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from opencode_manager.app import create_app
from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import AttemptFailed, JobFailed, _load_turn_baseline, run_opencode_job
from opencode_manager.opencode.serve import ServeHandle
from opencode_manager.settings import Settings

from tests.job_end_helpers import assistant_msg, dummy_handle, user_msg


PREVIOUS = "PREVIOUS JOB OUTPUT — must never be this job"
THIS_TURN = "THIS TURN ONLY"


class _BaselineClient:
    """Resumed or new session. Scripted list_messages failures, then a new stop."""

    def __init__(
        self,
        *,
        inbound_creates: bool = False,
        fail_lists: int = 0,
        fail_forever: bool = False,
        list_error: Optional[BaseException] = None,
        prior: Optional[list] = None,
        this_text: str = THIS_TURN,
    ) -> None:
        self.session_id = "ses_follow"
        self.inbound_creates = inbound_creates
        self.fail_lists_left = fail_lists
        self.fail_forever = fail_forever
        self.list_error = list_error or RuntimeError("list_messages down")
        self.list_calls = 0
        self.posts: list[str] = []
        self.aborted: list[str] = []
        self.serves = 0
        self.prior = prior if prior is not None else [
            user_msg("u0", "first ticket turn"),
            assistant_msg("a0", PREVIOUS, finish="stop"),
        ]
        self._messages = list(self.prior)
        self.this_text = this_text

    def close(self) -> None:
        return None

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)

    def health(self) -> bool:
        return True

    def wait_directory(self, timeout: float = 1.0, should_stop=None) -> None:  # noqa: ANN001, ARG002
        return None

    def list_known_models(self, timeout: float = 1.0) -> list[str]:  # noqa: ARG002
        return ["opencode/hy3-free"]

    def resume_or_create(self, inbound, title):  # noqa: ANN001, ARG002
        if self.inbound_creates:
            return self.session_id, True
        if inbound and str(inbound).startswith("ses_"):
            return str(inbound), False
        return self.session_id, True

    def status(self) -> dict:
        return {}

    def session_payload(self, session_id: str) -> dict:  # noqa: ARG002
        return {}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        self.list_calls += 1
        if self.fail_forever or self.fail_lists_left > 0:
            if self.fail_lists_left > 0:
                self.fail_lists_left -= 1
            raise self.list_error
        return list(self._messages)

    def post_message(self, session_id: str, text: str, model=None, agent=None) -> None:  # noqa: ANN001, ARG002
        self.posts.append(text)
        self._messages = list(self.prior) + [
            user_msg(f"u_new_{len(self.posts)}", text),
            assistant_msg(f"a_new_{len(self.posts)}", self.this_text, finish="stop"),
        ]


def _patch_loop(monkeypatch: pytest.MonkeyPatch, client: _BaselineClient) -> list[ServeHandle]:
    handles: list[ServeHandle] = []

    def start_serve(**kwargs):  # noqa: ANN003
        client.serves += 1
        handle = dummy_handle(pid=42000 + client.serves, port=52000 + client.serves)
        handles.append(handle)
        on_spawn = kwargs.get("on_spawn")
        if on_spawn:
            on_spawn(handle)
        return handle

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", start_serve)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", lambda *_a, **_k: client)
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry._backoff", lambda *_a, **_k: None)
    return handles


def _job(**kwargs) -> JobRecord:
    data = dict(
        job_id="job_base_follow",
        jira_id="BASE-1",
        status="running",
        live=True,
        session_id="ses_follow",
        prompt="follow-up prompt",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        retry_count=2,
        timeout_in_seconds=20,
    )
    data.update(kwargs)
    return JobRecord(**data)


def _run(tmp_settings: Settings, client: _BaselineClient, job: Optional[JobRecord] = None) -> tuple:
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / (job.jira_id if job else "BASE-1")
    clone.mkdir(parents=True, exist_ok=True)
    rec = job or _job()
    result = run_opencode_job(
        rec,
        settings=tmp_settings,
        store=store,
        clone=clone,
        should_stop=lambda: False,
    )
    return result, rec, client


def test_load_baseline_raises_transport_when_history_required() -> None:
    class _Boom:
        def list_messages(self, session_id: str) -> list:  # noqa: ARG002
            raise RuntimeError("down")

    with pytest.raises(AttemptFailed) as caught:
        _load_turn_baseline(_Boom(), "ses_x", must_have_history=True)
    assert caught.value.kind == "transport"
    assert "baseline" in caught.value.message


def test_load_baseline_empty_on_new_session_list_fail() -> None:
    class _Boom:
        def list_messages(self, session_id: str) -> list:  # noqa: ARG002
            raise RuntimeError("down")

    prior, floor = _load_turn_baseline(_Boom(), "ses_x", must_have_history=False)
    assert prior == []
    assert floor is None


def test_resume_list_always_fails_is_not_previous_success(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_forever=True)
    _patch_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as caught:
        _run(tmp_settings, client)
    assert caught.value.status_code == 500
    assert PREVIOUS not in (caught.value.message or "")
    assert client.posts == []


def test_resume_http_error_on_list_is_not_previous_success(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "http://127.0.0.1/session/ses_follow/message"),
        response=httpx.Response(503),
    )
    client = _BaselineClient(fail_forever=True, list_error=err)
    _patch_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as caught:
        _run(tmp_settings, client, _job(retry_count=1))
    assert caught.value.status_code == 500
    assert client.posts == []


def test_resume_timeout_on_list_is_not_previous_success(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_forever=True, list_error=httpx.TimeoutException("slow"))
    _patch_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as caught:
        _run(tmp_settings, client, _job(retry_count=1))
    assert caught.value.status_code == 500
    assert PREVIOUS not in str(caught.value)


def test_resume_list_fails_then_recovers_this_turn_only(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First attempt cannot see history; second lists and answers this turn."""
    client = _BaselineClient(fail_lists=1)
    _patch_loop(monkeypatch, client)
    result, job, client = _run(tmp_settings, client)
    assert result.status_code == 200
    assert result.text == THIS_TURN
    assert job.text == THIS_TURN
    assert PREVIOUS not in (result.text or "")
    assert job.attempts[0].kind == "transport"
    assert any(row.id == "ORIGINAL" for row in job.prompts)
    assert client.posts


def test_already_bound_list_fail_then_hang_resume_this_turn(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_lists=1)
    _patch_loop(monkeypatch, client)
    job = _job(session_bound=True, original_posted=True, retry_count=2)
    result, job, client = _run(tmp_settings, client, job)
    assert result.status_code == 200
    assert result.text == THIS_TURN
    assert PREVIOUS not in (result.text or "")
    assert any(row.id == "HANG_RESUME" for row in job.prompts)
    assert not any(row.id == "ORIGINAL" for row in job.prompts)


def test_new_session_list_fail_still_ships_this_turn(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(
        inbound_creates=True,
        fail_lists=1,
        prior=[],
    )
    _patch_loop(monkeypatch, client)
    result, job, _c = _run(tmp_settings, client, _job(session_id=""))
    assert result.status_code == 200
    assert result.text == THIS_TURN
    assert any(row.id == "ORIGINAL" for row in job.prompts)


def test_inbound_placeholder_creates_and_ignores_list_fail(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(inbound_creates=True, fail_lists=1, prior=[])
    _patch_loop(monkeypatch, client)
    result, _job_out, _c = _run(tmp_settings, client, _job(session_id="-1"))
    assert result.status_code == 200
    assert result.text == THIS_TURN


def test_resume_does_not_post_user_when_baseline_list_fails(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_forever=True)
    _patch_loop(monkeypatch, client)
    with pytest.raises(JobFailed):
        _run(tmp_settings, client, _job(retry_count=1))
    assert client.posts == []


def test_second_followup_list_fail_does_not_return_first_job_text(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Job B on the same ses_* must not callback job A's stop if list is down."""
    client = _BaselineClient(fail_forever=True)
    _patch_loop(monkeypatch, client)
    with pytest.raises(JobFailed) as caught:
        _run(
            tmp_settings,
            client,
            _job(job_id="job_b", jira_id="BASE-2", session_id="ses_follow", retry_count=1),
        )
    assert caught.value.status_code == 500
    assert PREVIOUS not in (caught.value.message or "")


def test_success_product_uses_this_turn_when_final_list_fails(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a real this-turn stop, a late list failure must not fall back to prior text."""
    job = _job()
    client = _BaselineClient()
    real_list = client.list_messages

    def flaky_list(session_id: str) -> list:
        if job.text == THIS_TURN:
            raise RuntimeError("list died after this-turn text latched")
        return real_list(session_id)

    client.list_messages = flaky_list  # type: ignore[method-assign]
    _patch_loop(monkeypatch, client)
    result, job, _c = _run(tmp_settings, client, job)
    assert result.status_code == 200
    assert result.text == THIS_TURN
    assert job.text == THIS_TURN
    assert PREVIOUS not in (result.text or "")


def _wait_terminal(http, job_id: str):
    last = None
    for _ in range(80):
        last = http.get(f"/jobs/{job_id}")
        if last.status_code == 200 and last.json().get("live") is False:
            return last
        time.sleep(0.05)
    return last


def _wire_api(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, client: _BaselineClient):
    _patch_loop(monkeypatch, client)

    def fake_clone(url, dest, branch, **kwargs):  # noqa: ANN001, ARG001
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("opencode_manager.worker.clone_repo", fake_clone)
    return client


def test_poller_followup_list_fail_is_500_not_previous_text(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_forever=True)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0

    body = {
        "repo_url": "https://gitlab.example/g/r.git",
        "source_branch": "develop",
        "prompt": "follow-up prompt",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 20,
        "retry_count": 1,
        "jira_id": "BASE-POLL",
        "session_id": "ses_follow",
        "callback_url": "",
    }
    with TestClient(create_app(tmp_settings)) as http:
        ack = http.post("/jobs", json=body)
        assert ack.status_code == 202
        job_id = ack.json()["job_id"]
        poll = _wait_terminal(http, job_id)
        assert poll is not None
        assert poll.status_code == 200
        envelope = poll.json()
        assert envelope["status_code"] == 500
        assert envelope["live"] is False
        assert PREVIOUS not in (envelope.get("text") or "")
        assert "previous job" not in (envelope.get("text") or "").lower()


def test_poller_followup_recovers_this_turn_only(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_lists=1)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0

    body = {
        "repo_url": "https://gitlab.example/g/r.git",
        "prompt": "follow-up prompt",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 20,
        "retry_count": 2,
        "jira_id": "BASE-POLL2",
        "session_id": "ses_follow",
        "callback_url": "",
    }
    with TestClient(create_app(tmp_settings)) as http:
        ack = http.post("/jobs", json=body)
        assert ack.status_code == 202
        poll = _wait_terminal(http, ack.json()["job_id"])
        assert poll.json()["status_code"] == 200
        assert poll.json()["text"] == THIS_TURN
        assert PREVIOUS not in poll.json()["text"]


def test_callback_followup_list_fail_does_not_post_previous_text(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: List[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            posted.append(loads(raw.decode("utf-8") or "{}"))
            self.send_response(200)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = _BaselineClient(fail_forever=True)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0

    try:
        body = {
            "repo_url": "https://gitlab.example/g/r.git",
            "prompt": "follow-up prompt",
            "model": "opencode/hy3-free",
            "agent_mode": "orchestrator",
            "timeout_in_seconds": 20,
            "retry_count": 1,
            "jira_id": "BASE-CB",
            "session_id": "ses_follow",
            "callback_url": f"http://127.0.0.1:{server.server_address[1]}/wait",
        }
        with TestClient(create_app(tmp_settings)) as http:
            ack = http.post("/jobs", json=body)
            assert ack.status_code == 202
            _wait_terminal(http, ack.json()["job_id"])
    finally:
        server.shutdown()
    assert posted
    assert posted[0]["status_code"] == 500
    assert PREVIOUS not in (posted[0].get("text") or "")


def test_callback_followup_recover_posts_this_turn_only(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: List[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            posted.append(loads(self.rfile.read(length).decode("utf-8") or "{}"))
            self.send_response(200)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = _BaselineClient(fail_lists=1)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0

    try:
        body = {
            "repo_url": "https://gitlab.example/g/r.git",
            "prompt": "follow-up prompt",
            "model": "opencode/hy3-free",
            "agent_mode": "orchestrator",
            "timeout_in_seconds": 20,
            "retry_count": 2,
            "jira_id": "BASE-CB2",
            "session_id": "ses_follow",
            "callback_url": f"http://127.0.0.1:{server.server_address[1]}/wait",
        }
        with TestClient(create_app(tmp_settings)) as http:
            ack = http.post("/jobs", json=body)
            _wait_terminal(http, ack.json()["job_id"])
    finally:
        server.shutdown()
    assert posted
    assert posted[0]["status_code"] == 200
    assert posted[0]["text"] == THIS_TURN
    assert PREVIOUS not in posted[0]["text"]


def test_dashboard_job_record_does_not_store_previous_text_on_list_fail(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _BaselineClient(fail_forever=True)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0
    body = {
        "repo_url": "https://gitlab.example/g/r.git",
        "prompt": "follow-up prompt",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 20,
        "retry_count": 1,
        "jira_id": "BASE-UI",
        "session_id": "ses_follow",
        "callback_url": "",
    }
    with TestClient(create_app(tmp_settings)) as http:
        ack = http.post("/jobs", json=body)
        job_id = ack.json()["job_id"]
        _wait_terminal(http, job_id)
        detail = http.get(f"/api/jobs/{job_id}").json()["job"]
        assert detail["status"] == "error"
        assert PREVIOUS not in (detail.get("text") or "")
        assert PREVIOUS not in (detail.get("error_message") or "")
        chat = http.get(f"/api/jobs/{job_id}/chat").json()
        blob = str(chat.get("messages") or "")
        assert PREVIOUS not in blob


def test_opencode_runner_followup_list_fail_returns_500_not_previous(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencode_manager.worker import OpenCodeRunner

    client = _BaselineClient(fail_forever=True)
    _wire_api(tmp_settings, monkeypatch, client)
    tmp_settings.retry_backoff_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    runner = OpenCodeRunner(tmp_settings, store)
    job = _job(retry_count=1)
    terminal = runner.run(job, should_stop=lambda: False)
    assert terminal.status_code == 500
    assert PREVIOUS not in (terminal.text or "")
    assert client.posts == []
