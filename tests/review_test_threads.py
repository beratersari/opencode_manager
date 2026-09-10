"""Match later /review findings to existing unresolved Creasy threads."""

from __future__ import annotations

from opencode_manager.review.findings import Finding
from opencode_manager.review.position import CREASY_FINDING_MARK, format_discussion
from opencode_manager.review.similarity import should_skip_similar_reply, text_similarity
from opencode_manager.review.threads import match_creasy_thread, parse_creasy_thread, parse_creasy_threads


def _finding(**kwargs) -> Finding:
    data = dict(
        path="src/buf.cpp",
        start_line=40,
        end_line=44,
        side="new",
        severity="critical",
        title="overflow",
        body="strcpy overflows",
    )
    data.update(kwargs)
    return Finding(**data)


def _raw(
    *,
    disc_id: str = "disc_1",
    path: str = "src/buf.cpp",
    new_line: int = 42,
    end_line: int | None = None,
    body: str = f"{CREASY_FINDING_MARK}\n**Critical** · overflow",
    resolved: bool = False,
    individual: bool = False,
    old_path: str | None = None,
    old_line: int | None = None,
) -> dict:
    pos: dict = {
        "new_path": path,
        "old_path": old_path if old_path is not None else path,
        "new_line": new_line,
    }
    if old_line is not None:
        pos["old_line"] = old_line
        if new_line == 0:
            pos.pop("new_line", None)
    if end_line is not None:
        pos["line_range"] = {
            "start": {"new_line": new_line, "type": "new"},
            "end": {"new_line": end_line, "type": "new"},
        }
    return {
        "id": disc_id,
        "individual_note": individual,
        "notes": [{"body": body, "resolved": resolved, "position": pos}],
    }


def test_format_discussion_marks_creasy_thread() -> None:
    text = format_discussion(_finding())
    assert CREASY_FINDING_MARK in text
    assert "**Critical**" in text


def test_parse_skips_human_and_resolved_and_notes() -> None:
    assert parse_creasy_thread(_raw(body="looks wrong to me")) is None
    assert parse_creasy_thread(_raw(resolved=True)) is not None
    assert parse_creasy_thread(_raw(resolved=True)).resolved is True
    assert parse_creasy_thread(_raw(individual=True)) is None
    old = parse_creasy_thread(
        {
            "id": "legacy",
            "notes": [
                {
                    "body": "**Major** · leak",
                    "resolved": False,
                    "position": {"new_path": "a.c", "new_line": 3},
                }
            ],
        }
    )
    assert old is not None
    assert old.path == "a.c"
    assert old.start_line == 3
    assert old.end_line == 3


def test_match_overlaps_same_path_unresolved() -> None:
    threads = parse_creasy_threads([_raw(new_line=40, end_line=44)])
    hit = match_creasy_thread(_finding(start_line=42, end_line=46), threads, set())
    assert hit is not None
    assert hit.discussion_id == "disc_1"


def test_match_skips_resolved_other_path_and_used() -> None:
    threads = parse_creasy_threads(
        [
            _raw(disc_id="done", new_line=40, end_line=44, resolved=True),
            _raw(disc_id="other", path="src/other.cpp", new_line=40, end_line=44),
            _raw(disc_id="live", new_line=40, end_line=44),
        ]
    )
    assert match_creasy_thread(_finding(), threads, {"live"}) is None
    hit = match_creasy_thread(_finding(), threads, set())
    assert hit is not None
    assert hit.discussion_id == "live"


def test_match_picks_tightest_overlap() -> None:
    threads = parse_creasy_threads(
        [
            _raw(disc_id="wide", new_line=1, end_line=80),
            _raw(disc_id="tight", new_line=40, end_line=44),
        ]
    )
    hit = match_creasy_thread(_finding(), threads, set())
    assert hit is not None
    assert hit.discussion_id == "tight"


def test_no_overlap_and_side_mismatch() -> None:
    threads = parse_creasy_threads([_raw(new_line=80, end_line=90)])
    assert match_creasy_thread(_finding(), threads, set()) is None
    old_only = parse_creasy_threads(
        [_raw(new_line=0, old_line=40, body=f"{CREASY_FINDING_MARK}\n**Critical** · x")]
    )
    # new_line 0 falls through to old_line in parser
    assert old_only
    assert old_only[0].side == "old"
    assert match_creasy_thread(_finding(side="new"), old_only, set()) is None
    assert match_creasy_thread(_finding(side="old", start_line=40, end_line=40), old_only, set()) is not None


def test_parse_keeps_last_creasy_note_body() -> None:
    raw = _raw()
    raw["notes"].append(
        {"body": f"{CREASY_FINDING_MARK}\n**Critical** · overflow\n\nstill overflows", "resolved": False}
    )
    thread = parse_creasy_thread(raw)
    assert thread is not None
    assert "still overflows" in thread.last_body


def test_identical_notes_are_above_similarity_threshold() -> None:
    text = format_discussion(_finding())
    assert text_similarity(text, text) >= 0.90
    assert should_skip_similar_reply(text, text)


def test_near_duplicate_long_notes_skip() -> None:
    base = format_discussion(
        _finding(
            body=(
                "strcpy copies src into an 8-byte stack buffer with no length check. "
                "main calls it with a 34-character string so every run overflows dest."
            )
        )
    )
    tweaked = base.replace("every run overflows dest", "every run overflows dest.")
    assert should_skip_similar_reply(tweaked, base)


def test_different_finding_does_not_skip() -> None:
    old = format_discussion(_finding(title="overflow", body="strcpy overflows dest"))
    new = format_discussion(_finding(title="use after free", body="delete then dereference ptr"))
    assert text_similarity(old, new) < 0.90
    assert not should_skip_similar_reply(new, old)


def test_empty_last_body_does_not_skip() -> None:
    assert not should_skip_similar_reply(format_discussion(_finding()), "")
