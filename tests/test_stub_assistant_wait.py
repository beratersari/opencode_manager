"""Empty OpenCode stub assistants must not spend an incomplete retry."""

from __future__ import annotations

import time

from opencode_manager.models import JobRecord
from opencode_manager.opencode import prompts
from opencode_manager.opencode.retry import _inner_loop, _post_user
from opencode_manager.opencode.session import assistant_turn_is_substantive
from opencode_manager.settings import Settings


def _job(**kwargs) -> JobRecord:
    data = dict(
        job_id="job_stub",
        jira_id="STUB-1",
        status="running",
        live=True,
        session_id="ses_stub",
        model="opencode/mimo-v2.5-free",
        agent_mode="orchestrator",
        prompt="do work",
        timeout_in_seconds=30,
        retry_count=3,
    )
    data.update(kwargs)
    return JobRecord(**data)


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


def test_stub_assistant_is_not_substantive() -> None:
    messages = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
        {"id": "a_stub", "info": {"role": "assistant", "id": "a_stub"}, "parts": []},
    ]
    assert not assistant_turn_is_substantive(messages, "")
    assert assistant_turn_is_substantive(
        [
            *messages,
            {
                "id": "a2",
                "info": {"role": "assistant", "id": "a2", "finish": "stop"},
                "parts": [{"type": "text", "text": "done"}],
            },
        ],
        "",
    )
    assert assistant_turn_is_substantive(
        [
            {
                "id": "a3",
                "info": {"role": "assistant", "id": "a3", "finish": "tool-calls"},
                "parts": [{"type": "text", "text": "calling"}],
            }
        ],
        "",
    )


class _StubThenWork:
    """Idle stub, then busy, then a clean stop — the live-storm 3.5% path."""

    def __init__(self) -> None:
        self.ticks = 0
        self.posts: list[str] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
        self.ticks += 1
        if self.ticks < 4:
            return {}
        if self.ticks < 7:
            return {"ses_stub": {"type": "busy"}}
        return {}

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        user = {
            "id": "u1",
            "info": {"role": "user", "id": "u1"},
            "parts": [{"type": "text", "text": "do it"}],
        }
        stub = {"id": "a_stub", "info": {"role": "assistant", "id": "a_stub"}, "parts": []}
        done = {
            "id": "a_done",
            "info": {"role": "assistant", "id": "a_done", "finish": "stop"},
            "parts": [{"type": "text", "text": "THIS TURN ONLY"}],
        }
        if self.ticks < 7:
            return [user, stub]
        return [user, stub, done]

    def abort(self, session_id: str) -> None:  # noqa: ARG002
        return None

    def post_message(self, session_id: str, text: str, *, model: str, agent: str) -> None:  # noqa: ARG002
        self.posts.append(text)


def test_inner_loop_waits_out_stub_then_succeeds(tmp_settings: Settings) -> None:
    client = _StubThenWork()
    job = _job()
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 5.0,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "success"
    assert job.text == "THIS TURN ONLY"
    assert client.posts == []


class _BusyThenIdle:
    def __init__(self) -> None:
        self.calls = 0
        self.posts: list[str] = []

    def status(self) -> dict:
        self.calls += 1
        if self.calls < 4:
            return {"ses_stub": {"type": "busy"}}
        return {}

    def post_message(self, session_id: str, text: str, *, model: str, agent: str) -> None:  # noqa: ARG002
        self.posts.append(text)


def test_incomplete_resume_waits_until_idle_then_posts() -> None:
    client = _BusyThenIdle()
    job = _job()
    _post_user(
        job,
        client,
        _MemStore(),
        "INCOMPLETE_RESUME",
        prompts.INCOMPLETE_RESUME,
        wait_busy_seconds=5.0,
    )
    assert client.posts == [prompts.INCOMPLETE_RESUME]
    assert client.calls >= 4


def test_other_prompts_still_refuse_busy_immediately() -> None:
    from opencode_manager.opencode.retry import AttemptFailed

    class _Busy:
        def status(self) -> dict:
            return {"ses_stub": {"type": "busy"}}

        def post_message(self, *a, **k) -> None:  # noqa: ANN002, ARG002
            raise AssertionError("must not POST")

    try:
        _post_user(_job(), _Busy(), _MemStore(), "ORIGINAL", "hello", wait_busy_seconds=0.0)
    except AttemptFailed as exc:
        assert exc.kind == "hang"
        assert "busy" in exc.message
    else:
        raise AssertionError("expected AttemptFailed")
