from __future__ import annotations

from opencode_manager.gitlab.client import MergeRequest
from opencode_manager.review.prompt import build_ask_prompt, build_review_prompt, build_thread_review_prompt
from opencode_manager.workspace.gitops import DiffIndex


def _mr(**kwargs) -> MergeRequest:
    data = dict(
        project_id=1,
        iid=2,
        title="Add login",
        description="desc",
        author="berat",
        source_branch="feat",
        target_branch="main",
        sha="aaa",
        base_sha="bbb",
        start_sha="bbb",
        web_url="http://gl/mr/2",
        http_url="http://gl/repo.git",
        draft=False,
        state="opened",
        labels=["backend", "auth"],
        pipeline_status="failed",
        pipeline_url="http://gl/pipelines/9",
    )
    data.update(kwargs)
    return MergeRequest(**data)


def test_review_prompt_has_map_not_full_diff():
    index = DiffIndex(
        merge_base="bbb",
        stat=" src/a.py | 3 +++\n 1 file changed",
        paths=["src/a.py"],
        statuses={"src/a.py": "M"},
    )
    prompt = build_review_prompt(_mr(), index, extra_notes="be strict")
    assert "bbb" in prompt
    assert "src/a.py" in prompt
    assert "git diff bbb...HEAD" in prompt
    assert "Do not assume this prompt contains hunks" in prompt
    assert "@@ " not in prompt
    assert "Project review rules" not in prompt
    assert "be strict" in prompt
    assert "Draft: no" in prompt
    assert "`backend`" in prompt
    assert "`auth`" in prompt
    assert "Latest pipeline: failed (http://gl/pipelines/9)" in prompt
    assert "## MR description" in prompt
    assert "desc" in prompt
    assert "Required reply format" not in prompt
    assert "**Critical**" not in prompt
    assert "**Blocking**" not in prompt
    assert "**Why it is an issue and where**" not in prompt
    assert "**Suggested fix**" not in prompt


def test_ask_prompt_is_question():
    text = build_ask_prompt("why this lock?")
    assert text.startswith("why this lock?")
    assert "opencoderman-findings" in text
    with_parent = build_ask_prompt("why dest?", parent_text="Unbounded strcpy into dest.")
    assert "Unbounded strcpy into dest." in with_parent
    assert "Previous comment" in with_parent
    assert "why dest?" in with_parent
    with_ctx = build_ask_prompt("why?", mr=_mr(), index=DiffIndex("b", "stat", ["a.py"], {"a.py": "M"}), include_context=True)
    assert "why?" in with_ctx
    assert "Add login" in with_ctx
    assert "Draft: no" in with_ctx
    assert "`backend`" in with_ctx
    assert "failed" in with_ctx
    assert "Description: desc" in with_ctx
    moved = build_ask_prompt(
        "why?",
        mr=_mr(),
        index=DiffIndex("b", "stat", ["a.py"], {"a.py": "M"}),
        sha_changed=True,
        previous_sha="oldsha",
    )
    assert "oldsha" in moved
    assert "aaa" in moved
    assert "why?" in moved
    assert "HEAD moved" in moved
    assert "the MR HEAD" not in moved
    html = build_ask_prompt(
        "why?",
        mr=_mr(description="<p>Watch <b>dest</b>.</p>"),
        index=DiffIndex("b", "stat", ["a.py"], {"a.py": "M"}),
        include_context=True,
    )
    assert "Description: Watch dest." in html
    assert "<p>" not in html


def test_thread_review_prompt_includes_previous_comment() -> None:
    text = build_thread_review_prompt(
        _mr(),
        DiffIndex("b", "stat", ["a.py"], {"a.py": "M"}),
        user_text="why dest?",
        parent_text="Unbounded strcpy into dest.",
        path="src/app.py",
        start_line=2,
        end_line=2,
    )
    assert "Unbounded strcpy into dest." in text
    assert "why dest?" in text
    assert "`src/app.py`" in text
    assert "focused review" in text.lower()


def test_review_prompt_omits_empty_description_and_pipeline():
    index = DiffIndex(merge_base="bbb", stat="stat", paths=["a.py"], statuses={"a.py": "M"})
    prompt = build_review_prompt(_mr(description="", labels=[], pipeline_status="", pipeline_url=""), index)
    assert "Draft: no" in prompt
    assert "Labels: (none)" in prompt
    assert "Latest pipeline: (none)" in prompt
    assert "## MR description" not in prompt


def test_review_prompt_marks_draft_and_clips_long_description():
    index = DiffIndex(merge_base="bbb", stat="stat", paths=["a.py"], statuses={"a.py": "M"})
    prompt = build_review_prompt(_mr(draft=True, description="x" * 5000), index)
    assert "Draft: yes" in prompt
    assert "… (truncated)" in prompt
    assert "x" * 5000 not in prompt


def test_hang_resume_is_not_the_review_prompt():
    from opencode_manager.review.prompt import hang_resume_prompt

    text = hang_resume_prompt()
    assert "already posted" in text.lower()
    assert "@@ " not in text
    assert "Do not assume this prompt contains hunks" not in text
