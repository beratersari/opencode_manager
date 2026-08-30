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
    def __init__(self, *, status: dict, messages: list) -> None:
        self._status = status
        self._messages = messages
        self.aborts: list[str] = []
        self.posted: list[tuple[str, str]] = []

    def health(self) -> bool:
        return True

    def status(self) -> dict:
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
