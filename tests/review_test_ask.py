from opencode_manager.review.ask import ask_wants_new_review


def test_plain_question_is_not_a_new_review() -> None:
    assert ask_wants_new_review("Does this assume C++17?") is False
    assert ask_wants_new_review("why this lock?") is False
    assert ask_wants_new_review("can you review why dest is 8?") is False


def test_explicit_new_review_request() -> None:
    assert ask_wants_new_review("please do a new review") is True
    assert ask_wants_new_review("run a full review of this MR") is True
    assert ask_wants_new_review("re-review after my push") is True
    assert ask_wants_new_review("yeni inceleme yap") is True