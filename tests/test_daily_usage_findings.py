"""Tests for critical daily-usage findings from the five-area review."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_manager.log import redact
from opencode_manager.review_fifo import JobQueue
from opencode_manager.review_log import redact_userinfo
from opencode_manager.review_session import OpenCodeClient
from opencode_manager.workspace.gitops import public_git_url


def test_redact_query_tokens_and_slash_in_userinfo() -> None:
    query = redact("https://gitlab.example/g/r.git?private_token=glpat-LEAKME")
    assert "glpat-LEAKME" not in query
    assert "private_token=***" in query
    azure = redact("https://orga:zj3k/P+q1w2==@tfs02.example/tfs/DefaultCollection/p/_git/r")
    assert "zj3k/P+q1w2==" not in azure
    assert "***:***@" in azure
    env = redact("ANTHROPIC_API_KEY=sk-ant-api03-LEAKME")
    assert "sk-ant-api03-LEAKME" not in env
    assert "ANTHROPIC_API_KEY=***" in env or "sk-***" in env


def test_redact_userinfo_allows_slash_in_pat() -> None:
    text = redact_userinfo("clone https://zj3k/P+q1w2==@tfs02.example/tfs/App/_git/app")
    assert "zj3k/P+q1w2==" not in text
    assert "tfs02.example" in text


def test_review_fifo_persist_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "review_queue.json"
    queue = JobQueue(path)
    called: list[Path] = []

    def fake_atomic(dest: Path, text: str, **_k) -> None:
        called.append(dest)
        dest.write_text(text, encoding="utf-8")

    with patch("opencode_manager.review_fifo.write_text_atomic", fake_atomic):
        queue.enqueue("109-3", "job_a")
    assert called == [path]
    assert json.loads(path.read_text(encoding="utf-8")) == {"109-3": ["job_a"]}


def test_azure_clone_argv_has_no_pat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opencode_manager.workspace import gitops as gitops_mod

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(list(cmd))
        if "clone" in cmd:
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    dest = tmp_path / "ws"
    gitops_mod.clone_repo(
        "https://ado.example/tfs/DefaultCollection/App/_git/app",
        dest,
        "secret-pat-DAILY",
        timeout=5,
        auth_scheme="azure",
    )
    blob = " ".join(" ".join(row) for row in captured)
    assert "secret-pat-DAILY" not in blob
    assert "pat:secret-pat" not in blob
    assert public_git_url("https://ado.example/tfs/DefaultCollection/App/_git/app") in blob


def test_review_session_busy_when_compacting() -> None:
    client = OpenCodeClient("http://127.0.0.1:9", "C:/osm/workspaces/1-1")

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"time": {"compacting": 123}}

    client.status = lambda: {"type": "idle"}  # type: ignore[method-assign]
    client.get_session = lambda _sid: _Resp()  # type: ignore[method-assign]
    try:
        assert client.session_busy("ses_1") is True
    finally:
        client.close()
