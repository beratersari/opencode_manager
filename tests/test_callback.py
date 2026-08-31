"""Callback HTTP: 2xx delivered; retry Wait 404 / 5xx; stop on permanent 4xx."""

from __future__ import annotations

from typing import Any, List

import pytest

from opencode_manager.callback import callback_http_outcome, post_callback
from opencode_manager.models import Envelope
from opencode_manager.settings import Settings


def _envelope() -> Envelope:
    return Envelope(
        text="source_branch not on remote",
        session_id="",
        status_code=404,
        jira_id="KAN-11",
        job_id="job_deadbeef",
    )


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _ScriptedClient:
    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.posts: List[Any] = []

    def __enter__(self) -> "_ScriptedClient":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def post(self, url: str, json: Any = None) -> _Resp:
        self.posts.append((url, json))
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(int(nxt))


@pytest.mark.parametrize(
    ("status", "want"),
    [
        (200, "delivered"),
        (204, "delivered"),
        (404, "retry"),
        (408, "retry"),
        (429, "retry"),
        (500, "retry"),
        (503, "retry"),
        (400, "permanent"),
        (401, "permanent"),
        (403, "permanent"),
        (405, "permanent"),
        (410, "permanent"),
        (422, "permanent"),
    ],
)
def test_callback_http_outcome(status: int, want: str) -> None:
    assert callback_http_outcome(status) == want


def _run(
    tmp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    script: List[Any],
    retries: int = 3,
) -> _ScriptedClient:
    tmp_settings.callback_retry_count = retries
    client = _ScriptedClient(script)
    monkeypatch.setattr(
        "opencode_manager.callback.httpx.Client",
        lambda **_k: client,
    )
    monkeypatch.setattr("opencode_manager.callback.time.sleep", lambda _s: None)
    post_callback(tmp_settings, _envelope(), "http://127.0.0.1:9/webhook-waiting/1")
    return client


def test_callback_200_stops_without_retry(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [200])
    assert len(client.posts) == 1
    assert client.posts[0][1]["status_code"] == 404
    assert client.posts[0][1]["jira_id"] == "KAN-11"


def test_callback_404_then_200_retries(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [404, 200])
    assert len(client.posts) == 2
    assert client.posts[0][1] == client.posts[1][1]


def test_callback_400_does_not_retry(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [400, 200])
    assert len(client.posts) == 1


def test_callback_405_does_not_retry(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [405, 200])
    assert len(client.posts) == 1


def test_callback_503_then_200_retries(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [503, 200])
    assert len(client.posts) == 2


def test_callback_404_exhausted_uses_retry_count(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(tmp_settings, monkeypatch, [404, 404, 404], retries=3)
    assert len(client.posts) == 3


def test_callback_network_error_then_200_retries(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _run(
        tmp_settings,
        monkeypatch,
        [ConnectionError("refused"), 200],
    )
    assert len(client.posts) == 2
