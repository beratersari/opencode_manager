from __future__ import annotations

from opencode_manager.review.comment_range import (
    format_code_comment_prompt,
    parse_azure_thread_context,
    parse_gitlab_position,
)
from opencode_manager.review.mention import user_comment_text


def test_parse_gitlab_line_range() -> None:
    path, side, start, end = parse_gitlab_position(
        {
            "new_path": "src/lock.cpp",
            "old_path": "src/lock.cpp",
            "new_line": 40,
            "line_range": {
                "start": {"new_line": 40},
                "end": {"new_line": 52},
            },
        }
    )
    assert path == "src/lock.cpp"
    assert side == "new"
    assert start == 40
    assert end == 52


def test_parse_azure_right_range() -> None:
    path, side, start, end = parse_azure_thread_context(
        {
            "filePath": "/src/lock.cpp",
            "rightFileStart": {"line": 40, "offset": 1},
            "rightFileEnd": {"line": 52, "offset": 1},
        }
    )
    assert path == "src/lock.cpp"
    assert side == "new"
    assert start == 40
    assert end == 52


def test_format_includes_user_text_and_range() -> None:
    text = format_code_comment_prompt(
        "is this lock safe?",
        path="src/lock.cpp",
        side="new",
        start_line=40,
        end_line=52,
    )
    assert "src/lock.cpp" in text
    assert "40-52" in text
    assert "is this lock safe?" in text
    assert format_code_comment_prompt("why?", path="", start_line=0) == "why?"


def test_user_comment_keeps_words_around_command() -> None:
    got = user_comment_text("This overflow looks wrong.\n@creasy /ask is this UB?", ["creasy"])
    assert "overflow" in got
    assert "UB" in got
    assert "/ask" not in got
    assert "@creasy" not in got
