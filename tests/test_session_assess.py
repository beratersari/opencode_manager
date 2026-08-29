from opencode_manager.opencode.session import (
    assess_idle,
    last_assistant_id,
    looks_like_question,
    session_is_busy,
    turn_has_new_assistant,
)


def test_busy_status_shapes() -> None:
    assert session_is_busy({"ses_1": {"type": "busy"}}, "ses_1")
    assert session_is_busy({"ses_1": {"type": "busy_compacting"}}, "ses_1")
    assert not session_is_busy({"ses_1": {"type": "idle"}}, "ses_1")


def test_question_detect() -> None:
    assert looks_like_question("Shall I create the file?")
    assert not looks_like_question("Created the file.")


def test_assess_stop_success() -> None:
    messages = [{"info": {"role": "assistant", "finish": "stop"}, "parts": [{"type": "text", "text": "done"}]}]
    assert assess_idle(messages) == "success"


def test_assess_incomplete_tool_calls() -> None:
    messages = [{"info": {"role": "assistant", "finish": "tool-calls"}, "parts": []}]
    assert assess_idle(messages) == "incomplete"


def test_resume_old_stop_is_not_a_new_turn() -> None:
    prior = [
        {"id": "u1", "info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "3+4"}]},
        {
            "id": "a1",
            "info": {"role": "assistant", "id": "a1", "finish": "stop"},
            "parts": [{"type": "text", "text": "done 3+4"}],
        },
    ]
    assert assess_idle(prior) == "success"
    assert last_assistant_id(prior) == "a1"
    assert not turn_has_new_assistant(prior, "a1")
    continued = prior + [
        {"id": "u2", "info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "now 3+5"}]},
    ]
    assert not turn_has_new_assistant(continued, "a1")
    after = continued + [
        {
            "id": "a2",
            "info": {"role": "assistant", "id": "a2", "finish": "stop"},
            "parts": [{"type": "text", "text": "done 3+5"}],
        },
    ]
    assert turn_has_new_assistant(after, "a1")
