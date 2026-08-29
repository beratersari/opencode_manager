from opencode_manager.opencode.session import assess_idle, looks_like_question, session_is_busy


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
