"""Parse an inline/range comment into a file + line span for the prompt."""

from __future__ import annotations

from typing import Any, Optional

from opencode_manager.review.threads import _as_line, _norm_path, _range_from_position


def parse_gitlab_position(pos: Any) -> tuple[str, str, int, int]:
    if not isinstance(pos, dict) or not pos:
        return "", "", 0, 0
    path = _norm_path(str(pos.get("new_path") or pos.get("old_path") or ""))
    span = _range_from_position(pos)
    if span is None:
        return path, "", 0, 0
    side, start, end = span
    return path, side, start, end


def parse_azure_thread_context(ctx: Any) -> tuple[str, str, int, int]:
    if not isinstance(ctx, dict) or not ctx:
        return "", "", 0, 0
    path = _norm_path(str(ctx.get("filePath") or ctx.get("file_path") or ""))
    right = ctx.get("rightFileStart") if isinstance(ctx.get("rightFileStart"), dict) else {}
    left = ctx.get("leftFileStart") if isinstance(ctx.get("leftFileStart"), dict) else {}
    right_end = ctx.get("rightFileEnd") if isinstance(ctx.get("rightFileEnd"), dict) else {}
    left_end = ctx.get("leftFileEnd") if isinstance(ctx.get("leftFileEnd"), dict) else {}
    if right.get("line"):
        start = _as_line(right.get("line"))
        end = _as_line(right_end.get("line")) or start
        return path, "new", start, end if end >= start else start
    if left.get("line"):
        start = _as_line(left.get("line"))
        end = _as_line(left_end.get("line")) or start
        return path, "old", start, end if end >= start else start
    return path, "", 0, 0


def format_code_comment_prompt(
    question: str,
    *,
    path: str = "",
    side: str = "",
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    text = (question or "").strip()
    if not path or start_line <= 0:
        return text
    end = end_line or start_line
    lines = str(start_line) if end == start_line else f"{start_line}-{end}"
    where = f"`{path}` lines {lines}"
    if side:
        where += f" ({side} side)"
    return (
        f"The user asked this on an inline comment on {where}.\n\n"
        f"Their comment:\n\n{text}\n\n"
        "Read that file around those lines and the enclosing function. "
        "Answer the question about this code. Quote the relevant lines. "
        "Do not review the whole change unless the question needs it."
    )
