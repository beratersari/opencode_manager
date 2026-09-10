from __future__ import annotations

from typing import Optional

from opencode_manager.gitlab.client import MergeRequest
from opencode_manager.workspace.gitops import DiffIndex


_DESC_LIMIT = 4000
_ASK_DESC_LIMIT = 400


def _clip(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit].rstrip() + "\n… (truncated)"


def format_mr_meta(mr: MergeRequest) -> str:
    labels = ", ".join(f"`{name}`" for name in mr.labels[:20]) or "(none)"
    status = (mr.pipeline_status or "").strip()
    if status and mr.pipeline_url:
        pipeline = f"{status} ({mr.pipeline_url})"
    elif status:
        pipeline = status
    else:
        pipeline = "(none)"
    return (
        f"Draft: {'yes' if mr.draft else 'no'}\n"
        f"Labels: {labels}\n"
        f"Latest pipeline: {pipeline}"
    )


def format_mr_description(mr: MergeRequest, *, limit: int = _DESC_LIMIT) -> str:
    from opencode_manager.review.mention import plain_comment

    return _clip(plain_comment(mr.description), limit)


def build_review_prompt(
    mr: MergeRequest,
    index: DiffIndex,
    *,
    extra_notes: str = "",
) -> str:
    paths = "\n".join(f"- `{path}` ({index.statuses.get(path, '?')})" for path in index.paths) or "- (none after filters)"
    extra = ""
    if extra_notes.strip():
        extra = f"\n## Reviewer notes\n\n{extra_notes.strip()}\n"
    desc = format_mr_description(mr)
    desc_block = f"\n## MR description\n\n{desc}\n" if desc else ""
    return f"""You are reviewing GitLab merge request !{mr.iid}: {mr.title}

Author: {mr.author}
Branches: `{mr.source_branch}` → `{mr.target_branch}`
HEAD sha: `{mr.sha}`
Separation point (merge-base): `{index.merge_base}`
MR URL: {mr.web_url}
{format_mr_meta(mr)}
{desc_block}
The working tree is the MR source at HEAD. Analyze **from the separation point**, not the whole repo history.

## Diff stat (`git diff --stat {index.merge_base}...HEAD`)

```
{index.stat or '(empty)'}
```

## Changed paths

{paths}
{extra}
## Instructions

1. Run `git log {index.merge_base}..HEAD` and `git diff {index.merge_base}...HEAD` (and per-path diffs) yourself. Do not assume this prompt contains hunks.
2. For each changed path, read the current file and its callers/tests. Review the change in context.
3. Do not commit, push, or edit files.
"""


def build_ask_prompt(
    question: str,
    *,
    mr: Optional[MergeRequest] = None,
    index: Optional[DiffIndex] = None,
    sha_changed: bool = False,
    previous_sha: str = "",
    include_context: bool = False,
    parent_text: str = "",
) -> str:
    parts: list[str] = []
    if sha_changed and index is not None:
        parts.append(
            f"Note: the HEAD moved"
            + (f" from `{previous_sha}`" if previous_sha else "")
            + f" to `{mr.sha if mr else ''}`. Updated stat:\n```\n{index.stat}\n```"
        )
    if include_context and mr is not None:
        paths = ", ".join(index.paths[:40]) if index else ""
        parts.append(
            f"!{mr.iid} {mr.title} (`{mr.source_branch}` → `{mr.target_branch}`). "
            f"Changed files: {paths or '(see git)'}."
        )
        parts.append(format_mr_meta(mr).replace("\n", "; "))
        desc = format_mr_description(mr, limit=_ASK_DESC_LIMIT)
        if desc:
            parts.append(f"Description: {desc}")
        if index:
            parts.append(f"Separation point: `{index.merge_base}`. Use `git diff {index.merge_base}...HEAD` if needed.")
    parent = (parent_text or "").strip()
    if parent:
        parts.append("## Previous comment (what they replied to)\n\n" + parent)
    parts.append(question.strip())
    parts.append(
        "Answer the question only. Do not emit an opencoderman-findings fence "
        "and do not start a new review. The host will post this as a thread reply. "
        "Do not quote or restate the previous comment. Do not @mention or ping anyone."
    )
    return "\n\n".join(p for p in parts if p)


def build_thread_review_prompt(
    mr: MergeRequest,
    index: DiffIndex,
    *,
    user_text: str = "",
    parent_text: str = "",
    path: str = "",
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    """Focused review of the thread the user replied on."""
    span = ""
    if path:
        if start_line and end_line and end_line != start_line:
            span = f"`{path}` lines {start_line}–{end_line}"
        elif start_line:
            span = f"`{path}` line {start_line}"
        else:
            span = f"`{path}`"
    parent = (parent_text or "").strip() or "(no previous comment loaded)"
    request = (user_text or "").strip() or "(no extra notes)"
    location = f"This thread is on {span}." if span else "This is a reply on an existing review thread."
    return f"""You are continuing a code review on GitLab merge request !{mr.iid}: {mr.title}

{location}
The user replied on that thread. Do a focused review of this location in the current MR (HEAD `{mr.sha}`, merge-base `{index.merge_base}`). Read the file and nearby callers. You may emit an opencoderman-findings fence only for this area, unless the user asked for a full-MR review.

## Previous comment (what they replied to)

{parent}

## User request

{request}

## Instructions

1. Address the previous comment and the user request first.
2. Run `git diff {index.merge_base}...HEAD` for this path if needed. Do not restate the whole MR.
3. Do not quote or restate the previous comment in the posted answer. Do not @mention or ping anyone.
4. Do not commit, push, or edit files.
"""


HANG_RESUME = (
    "Continue the previous turn. The last user message was already posted. "
    "Do not restart the review or repeat the full analysis. Finish your answer."
)


def hang_resume_prompt() -> str:
    return HANG_RESUME
