"""Optional callback_url + GET /jobs/{id} poller."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List

from opencode_manager.settings import Settings
from opencode_manager.worker import Terminal

from tests.test_api import FakeRunner, _body, _client


def test_omit_callback_url_is_202(tmp_settings: Settings) -> None:
    body = _body()
    del body["callback_url"]
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=body)
        assert res.status_code == 202
        assert res.json()["job_id"].startswith("job_")


def test_empty_callback_url_is_202(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.post("/jobs", json=_body(callback_url=""))
        assert res.status_code == 202


def _wait_terminal(client, job_id: str):
    last = None
    for _ in range(50):
        last = client.get(f"/jobs/{job_id}")
        if last.status_code == 200 and last.json().get("live") is False:
            return last
        time.sleep(0.05)
    return last


def test_no_callback_posted_when_url_omitted(tmp_settings: Settings) -> None:
    posted: List[int] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, ARG002
            return

        def do_POST(self) -> None:  # noqa: N802
            posted.append(1)
            self.send_response(200)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = _body()
        del body["callback_url"]
        with _client(tmp_settings) as client:
            res = client.post("/jobs", json=body)
            assert res.status_code == 202
            job_id = res.json()["job_id"]
            poll = _wait_terminal(client, job_id)
            assert poll.status_code == 200
            assert poll.json()["status_code"] == 200
            assert poll.json()["live"] is False
            assert poll.json()["text"] == "assistant says hi"
    finally:
        server.shutdown()
    assert posted == []


def test_poll_live_then_terminal(tmp_settings: Settings) -> None:
    hold = threading.Event()

    class Held(FakeRunner):
        def run(self, job, *, should_stop):  # noqa: ANN001
            hold.wait(timeout=5)
            return super().run(job, should_stop=should_stop)

    body = _body()
    del body["callback_url"]
    with _client(tmp_settings, Held()) as client:
        ack = client.post("/jobs", json=body)
        assert ack.status_code == 202
        job_id = ack.json()["job_id"]
        mid = client.get(f"/jobs/{job_id}")
        assert mid.status_code == 202
        assert mid.json()["status_code"] == 202
        assert mid.json()["live"] is True
        assert mid.json()["status"] in {"queued", "running"}
        hold.set()
        for _ in range(50):
            done = client.get(f"/jobs/{job_id}")
            if done.status_code == 200 and done.json().get("live") is False:
                break
        assert done.status_code == 200
        assert done.json()["status_code"] == 200
        assert done.json()["session_id"] == "ses_fake"
        assert done.json()["text"] == "assistant says hi"


def test_poll_unknown_job_404(tmp_settings: Settings) -> None:
    with _client(tmp_settings) as client:
        res = client.get("/jobs/job_missing")
        assert res.status_code == 404
        assert res.json()["status_code"] == 404
        assert res.json()["job_id"] == "job_missing"


def test_poll_terminal_error_code(tmp_settings: Settings) -> None:
    with _client(tmp_settings, FakeRunner(Terminal(504, "attempt clock"))) as client:
        body = _body()
        del body["callback_url"]
        ack = client.post("/jobs", json=body)
        job_id = ack.json()["job_id"]
        poll = _wait_terminal(client, job_id)
        assert poll.status_code == 200
        assert poll.json()["status_code"] == 504
        assert poll.json()["live"] is False
        assert "attempt clock" in poll.json()["text"]
