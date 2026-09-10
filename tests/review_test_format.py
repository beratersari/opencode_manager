from __future__ import annotations

from opencode_manager import __version__
from opencode_manager.gitlab.events import first_command
from opencode_manager.models import JobRecord, mint_job_id
from opencode_manager.review.format import (
    format_cancelled,
    format_failure,
    format_success,
    format_usage,
    soften_markdown,
    strip_at_mentions,
)
from opencode_manager.review.mention import is_usage_note


def _job(**kwargs) -> JobRecord:
    data = dict(
        job_id=mint_job_id(),
        jira_id="1-2",
        job_kind="review",
        mr_key="1-2",
        project_id=1,
        mr_iid=2,
        trigger="open",
        text="looks fine",
        model="opencode/x",
    )
    data.update(kwargs)
    return JobRecord(**data)


def test_soften_turns_headings_into_bold_and_drops_rules() -> None:
    raw = """# Code Review: MR !30

---

## Summary

Five defects.

### 1. `src/buf.cpp` — overflow

strcpy overflows.

```cpp
#define MAX 8
char dest[MAX];
```

## Looks good
"""
    got = soften_markdown(raw)
    assert got.startswith("### Summary")
    assert "# Code Review" not in got
    assert not any(line.startswith("## ") and not line.startswith("### ") for line in got.splitlines())
    assert "---" not in got
    assert "#### 1. `src/buf.cpp` — overflow" in got
    assert "#define MAX 8" in got
    assert "### Improvement" in got
    assert "\n\n\n" not in got


def test_soften_leaves_include_and_fences_alone() -> None:
    raw = """**Summary**

```cpp
#include <cstring>
#define MAX 8
```

#include is not a heading
"""
    got = soften_markdown(raw)
    assert "#include <cstring>" in got
    assert "#define MAX 8" in got
    assert got.startswith("### Summary")


def test_success_note_is_comment_sized() -> None:
    job = _job(text="# Review\n\n---\n\n## Summary\n\nLooks risky.")
    body = format_success(job)
    assert body.startswith(f"**aMIR-mini {__version__} — Review**")
    assert f"`{job.model}`" in body
    assert f"`{job.job_id}`" in body
    assert "## Creasy" not in body
    assert "### Summary" in body
    assert "Looks risky." in body
    assert first_command(body) is None


def test_soften_rewrites_old_labels_and_drops_preamble() -> None:
    raw = """Now I have all the information needed for a thorough review.

**Summary**

Five defects.

**Blocking**

1. overflow

**Should fix**

2. leak

**Nits**

3. cmake

**Looks good**

4. layout
"""
    got = soften_markdown(raw)
    assert got.startswith("### Summary")
    assert "Now I have" not in got
    assert "### Critical" in got
    assert "### Major" in got
    assert "### Minor" in got
    assert "### Improvement" in got
    assert "Blocking" not in got
    assert "Should fix" not in got


def test_soften_keeps_group_and_issue_heading_levels() -> None:
    raw = """### Summary

Two defects.

### Critical

#### 1. `src/buf.cpp:6` — overflow

**Code**
```cpp
strcpy(dest, src);
```
"""
    got = soften_markdown(raw)
    assert "### Summary" in got
    assert "### Critical" in got
    assert "#### 1. `src/buf.cpp:6` — overflow" in got
    assert "**Code**" in got


def test_soften_keeps_turkish_group_headers() -> None:
    raw = """### Özet

Bir Kritik.

### Kritik

#### 1. `src/buf.cpp:6` — overflow

**Kod**
```cpp
strcpy(dest, src);
```

**Sorun**
Sınırsız strcpy.

**Öneri**
std::string kullanın.
"""
    got = soften_markdown(raw)
    assert got.startswith("### Özet")
    assert "### Kritik" in got
    assert "**Kod**" in got
    assert "**Sorun**" in got
    assert "**Öneri**" in got


def test_ask_note_uses_answer_label() -> None:
    job = _job(trigger="ask", text="Because the lock is per MR.")
    body = format_success(job)
    assert body.startswith(f"**aMIR-mini {__version__} — Answer**")
    assert first_command(body) is None


def test_thread_reply_does_not_quote_previous_comment() -> None:
    job = _job(
        trigger="ask",
        text="Because dest is 8. See @mberatersari.",
        discussion_id="disc_1",
        comment_text="why dest?",
        parent_comment_text="Unbounded strcpy into dest.",
    )
    body = format_success(job)
    assert "**Replying to**" not in body
    assert "**Your request**" not in body
    assert "Unbounded strcpy into dest." not in body
    assert "Because dest is 8." in body
    assert "@mberatersari" not in body
    assert "mberatersari" in body
    assert strip_at_mentions("ping @bot please") == "ping bot please"


def test_usage_note_is_help_not_a_command() -> None:
    body = format_usage(_job(trigger="usage"))
    assert is_usage_note(body)
    assert "/ask" in body
    assert "/review" in body
    assert "how to run a command" in body


def test_failure_and_cancel_notes_are_not_commands() -> None:
    job = _job(error_message="boom")
    for body in (format_failure(job), format_cancelled(job)):
        assert body.startswith(f"**aMIR-mini {__version__} —")
        assert first_command(body) is None
        assert "## " not in body


def test_failure_note_redacts_oauth_token() -> None:
    token = "super-secret-gitlab-token-TESTONLY"
    job = _job(
        error_message=f"git failed (128): fatal: Authentication failed for 'https://oauth2:{token}@gitlab.example/group/repo.git/'"
    )
    body = format_failure(job)
    assert token not in body
    assert "oauth2:" not in body
    assert "https://gitlab.example/group/repo.git/" in body
