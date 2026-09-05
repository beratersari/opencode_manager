"""Follow-up job: killed serve is serve-dead, not success from the previous stop."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencode_manager.dashboard.store import JobStore
from opencode_manager.models import JobRecord
from opencode_manager.opencode.retry import JobFailed, run_opencode_job
from opencode_manager.opencode.serve import ServeHandle
from opencode_manager.settings import Settings

from tests.job_end_helpers import assistant_msg, dummy_handle, user_msg


PREVIOUS = "PREVIOUS JOB OUTPUT"
THIS_TURN = "THIS TURN ONLY"


class _FollowupKillClient:
    """Resumed ses_* still has the last job's stop. First serve dies after POST."""

    def __init__(self) -> None:
        self.session_id = "ses_follow"
        self.posts: list[str] = []
        self.aborted: list[str] = []
        self.health_ok = True
        self.serves = 0
        self.prior = [
            user_msg("u0", "first ticket turn"),
            assistant_msg("a0", PREVIOUS, finish="stop"),
        ]
        self._messages = list(self.prior)

    def close(self) -> None:
        return None

    def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)

    def health(self) -> bool:
        return self.health_ok

    def wait_directory(self, timeout: float = 1.0, should_stop=None) -> None:  # noqa: ANN001, ARG002
        return None

    def list_known_models(self, timeout: float = 1.0) -> list[str]:  # noqa: ARG002
        return ["opencode/hy3-free"]

    def resume_or_create(self, inbound, title):  # noqa: ANN001, ARG002
        sid = inbound if inbound and str(inbound).startswith("ses_") else self.session_id
        return sid, False

    def status(self) -> dict:
        return {}

    def session_payload(self, session_id: str) -> dict:  # noqa: ARG002
        return {}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        return list(self._messages)

    def post_message(self, session_id: str, text: str, model=None, agent=None) -> None:  # noqa: ANN001, ARG002
        self.posts.append(text)
        if len(self.posts) == 1:
            self._messages = self.prior + [user_msg("u1", text)]
            self.health_ok = False
            return
        self.health_ok = True
        self._messages = self.prior + [
            user_msg("u1", "follow-up prompt"),
            user_msg("u2", text),
            assistant_msg("a2", THIS_TURN, finish="stop"),
        ]


def _job() -> JobRecord:
    return JobRecord(
        job_id="job_follow_kill",
        jira_id="FU-KILL",
        status="running",
        live=True,
        session_id="ses_follow",
        prompt="follow-up prompt",
        model="opencode/hy3-free",
        agent_mode="orchestrator",
        retry_count=2,
        timeout_in_seconds=20,
    )


def test_inner_loop_killed_serve_is_not_prior_success(tmp_settings: Settings) -> None:
    from opencode_manager.opencode.retry import _inner_loop

    class _Dead:
        def health(self) -> bool:
            return False

        def status(self) -> dict:
            return {}

        def list_messages(self, session_id: str) -> list:  # noqa: ARG002
            return [
                user_msg("u0", "first"),
                assistant_msg("a0", PREVIOUS, finish="stop"),
                user_msg("u1", "follow-up"),
            ]

        def abort(self, session_id: str) -> None:  # noqa: ARG002
            return None

    job = _job()
    outcome = _inner_loop(
        job,
        _Dead(),
        JobStore(tmp_settings.job_store_dir),
        settings=tmp_settings,
        deadline=__import__("time").time() + 5.0,
        should_stop=lambda: False,
        baseline_assistant_id="a0",
        baseline_n=2,
        baseline_compact_n=0,
    )
    assert outcome == "serve-dead"
    assert job.text != PREVIOUS
    assert PREVIOUS not in (job.text or "")


def test_killed_serve_retries_new_serve_not_previous_text(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_settings.retry_backoff_seconds = 0.0
    tmp_settings.retry_backoff_cap_seconds = 0.0
    store = JobStore(tmp_settings.job_store_dir)
    clone = tmp_settings.work_dir / "FU-KILL"
    clone.mkdir(parents=True)
    client = _FollowupKillClient()
    handles: list[ServeHandle] = []

    def start_serve(**kwargs):  # noqa: ANN003
        client.serves += 1
        client.health_ok = True
        handle = dummy_handle(pid=41000 + client.serves, port=51000 + client.serves)
        handles.append(handle)
        on_spawn = kwargs.get("on_spawn")
        if on_spawn:
            on_spawn(handle)
        return handle

    monkeypatch.setattr("opencode_manager.opencode.retry.start_serve", start_serve)
    monkeypatch.setattr("opencode_manager.opencode.retry.OpenCodeClient", lambda *_a, **_k: client)
    monkeypatch.setattr("opencode_manager.opencode.retry.stop_serve", lambda *_a, **_k: None)
    monkeypatch.setattr("opencode_manager.opencode.retry.kill_pid", lambda *_a, **_k: None)

    job = _job()
    result = run_opencode_job(
        job,
        settings=tmp_settings,
        store=store,
        clone=clone,
        should_stop=lambda: False,
    )
    assert result.status_code == 200
    assert result.text == THIS_TURN
    assert job.text == THIS_TURN
    assert PREVIOUS not in (result.text or "")
    assert client.serves == 2
    assert [h.pid for h in handles] == [41001, 41002]
    assert [h.port for h in handles] == [51001, 51002]
    assert job.attempts[0].kind == "serve-dead"
    assert any(row.id == "HANG_RESUME" for row in job.prompts)
    assert any(row.id == "ORIGINAL" for row in job.prompts)
