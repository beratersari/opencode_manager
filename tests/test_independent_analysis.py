"""Compact-loop counts new markers this wait; hang-after-assistant is intentional."""

from __future__ import annotations

import time

from opencode_manager.models import JobRecord
from opencode_manager.opencode.prompts import COMPACT_LOOP_NUDGE
from opencode_manager.opencode.retry import _COMPACT_LOOP_NEW, _inner_loop
from opencode_manager.settings import Settings


class _MemStore:
    def save(self, job: JobRecord) -> None:
        return None


class _LoopClient:
    def __init__(self, *, status: dict, messages: list, busy_polls: int = 0) -> None:
        self._status = status
        self._messages = messages
        self._busy_polls = busy_polls
        self._polls = 0
        self.aborts: list[str] = []
        self.posted: list[tuple[str, str]] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
        self._polls += 1
        if self._polls <= self._busy_polls:
            return {"ses_analysis": {"type": "busy"}}
        return self._status

    def list_messages(self, session_id: str) -> list:  # noqa: ARG002
        return list(self._messages)

    def abort(self, session_id: str) -> None:
        self.aborts.append(session_id)

    def post_message(self, session_id: str, text: str, *, model: str, agent: str) -> None:  # noqa: ARG002
        self.posted.append((session_id, text))


def _job(**kwargs) -> JobRecord:
    data = dict(
        job_id="job_analysis",
        jira_id="AN-1",
        status="running",
        live=True,
        session_id="ses_analysis",
        model="opencode/hy3-free",
        agent_mode="build",
        prompt="do the work",
        timeout_in_seconds=30,
        retry_count=3,
    )
    data.update(kwargs)
    return JobRecord(**data)


def test_hang_does_not_fire_after_assistant_this_turn(tmp_settings: Settings) -> None:
    """Intentional: hang is 'never started answering'. First assistant = progress."""
    tmp_settings.hang_timeout_seconds = 0.2
    messages = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do it"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1"},
            "parts": [{"type": "text", "text": "working on it"}],
        },
    ]
    client = _LoopClient(status={"ses_analysis": {"type": "busy"}}, messages=messages)
    job = _job()
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 0.7,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "timeout"


def test_hang_fires_when_busy_with_no_assistant_this_turn(tmp_settings: Settings) -> None:
    tmp_settings.hang_timeout_seconds = 0.2
    messages = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do it"}]},
    ]
    client = _LoopClient(status={"ses_analysis": {"type": "busy"}}, messages=messages)
    job = _job()
    started = time.time()
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 2.5,
        should_stop=lambda: False,
        baseline_assistant_id="",
        baseline_n=1,
        baseline_compact_n=0,
    )
    assert outcome == "hang"
    assert time.time() - started < 1.5


def test_historical_compact_markers_do_not_trigger_compact_loop_nudge(tmp_settings: Settings) -> None:
    """PLAN §5.3: compact-loop is ~8 *new* compact cycles this wait, not history.

    Resuming a long ticket session (same path, old ses_*) often already
    has many compact markers. That must not abort the turn.
    """
    history = []
    for i in range(8):
        history.append(
            {
                "id": f"c{i}",
                "info": {"role": "system", "id": f"c{i}"},
                "parts": [{"type": "compact", "text": "Session auto-compacted"}],
            }
        )
    history.append(
        {
            "id": "a_now",
            "info": {"role": "assistant", "id": "a_now", "finish": "tool-calls"},
            "parts": [{"type": "text", "text": "calling a tool"}],
        }
    )
    client = _LoopClient(status={}, messages=history)
    job = _job()
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 3.0,
        should_stop=lambda: False,
        baseline_assistant_id="a_old",
        baseline_n=8,
        baseline_compact_n=8,
    )
    assert COMPACT_LOOP_NUDGE not in [t for _, t in client.posted]
    assert outcome == "incomplete", (
        f"historical compact markers changed the idle verdict to {outcome!r} "
        f"and posted {client.posted!r}"
    )


def test_eight_new_compact_markers_this_wait_trigger_nudge(tmp_settings: Settings) -> None:
    messages = [
        {
            "id": "a_now",
            "info": {"role": "assistant", "id": "a_now", "finish": "tool-calls"},
            "parts": [{"type": "text", "text": "calling a tool"}],
        }
    ]
    for i in range(_COMPACT_LOOP_NEW):
        messages.insert(
            0,
            {
                "id": f"c_new{i}",
                "info": {"role": "system", "id": f"c_new{i}"},
                "parts": [{"type": "compact", "text": "Session auto-compacted"}],
            },
        )
    client = _LoopClient(status={}, messages=messages)
    job = _job()
    _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 3.0,
        should_stop=lambda: False,
        baseline_assistant_id="a_old",
        baseline_n=0,
        baseline_compact_n=0,
    )
    assert any(text == COMPACT_LOOP_NUDGE for _, text in client.posted)


def test_resumed_session_idle_old_stop_is_not_this_job_success(tmp_settings: Settings) -> None:
    """Follow-up job must not ship the previous job's finish=stop text."""
    prior = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "first"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1", "finish": "stop"},
            "parts": [{"type": "text", "text": "previous job output"}],
        },
        {"id": "u2", "info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "follow up"}]},
    ]
    # Busy once so awaiting_turn drops, then idle on the previous stop.
    client = _LoopClient(status={}, messages=prior, busy_polls=1)
    job = _job(original_posted=True)
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 3.0,
        should_stop=lambda: False,
        baseline_assistant_id="a1",
        baseline_n=2,
        baseline_compact_n=0,
    )
    assert outcome == "incomplete"
    assert job.text != "previous job output"


def test_this_turn_text_does_not_copy_prior_assistant(tmp_settings: Settings) -> None:
    messages = [
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1", "finish": "stop"},
            "parts": [{"type": "text", "text": "previous job output"}],
        },
        {"id": "u2", "info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "again"}]},
        {
            "id": "a2",
            "info": {"role": "assistant", "id": "a2", "finish": "stop"},
            "parts": [{"type": "text", "text": "this turn only"}],
        },
    ]
    client = _LoopClient(status={}, messages=messages)
    job = _job()
    outcome = _inner_loop(
        job,
        client,
        _MemStore(),
        settings=tmp_settings,
        deadline=time.time() + 3.0,
        should_stop=lambda: False,
        baseline_assistant_id="a1",
        baseline_n=2,
        baseline_compact_n=0,
    )
    assert outcome == "success"
    assert job.text == "this turn only"
