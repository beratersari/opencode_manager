"""Daily-usage probes against real OpenCode 1.18.10 and review turn parsing."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from opencode_manager.review_session import HANG_RESUME, OpenCodeClient, turn_assistant_text


def _prev_review_messages() -> list[dict[str, Any]]:
    return [
        {
            "id": "u_old",
            "info": {"role": "user", "id": "u_old"},
            "parts": [{"type": "text", "text": "review this MR"}],
        },
        {
            "id": "a_old",
            "info": {"role": "assistant", "id": "a_old", "finish": "stop"},
            "parts": [
                {
                    "type": "text",
                    "text": "### Summary\nPrevious review body that must not be this /ask.",
                }
            ],
        },
    ]


def test_turn_assistant_text_without_new_user_is_previous_review() -> None:
    text = turn_assistant_text(_prev_review_messages(), prefer_review=False)
    assert "Previous review body" in text


def test_review_turn_text_is_empty_after_new_user_until_assistant() -> None:
    """Once this /ask user message is in the list, previous review is not this turn."""
    ask = "what does this lock do?"
    messages = _prev_review_messages() + [
        {
            "id": "u_ask",
            "info": {"role": "user", "id": "u_ask"},
            "parts": [{"type": "text", "text": ask}],
        }
    ]
    assert turn_assistant_text(messages, prefer_review=False) == ""
    assert HANG_RESUME.strip() not in ask


@pytest.mark.live
def test_live_opencode_message_limit_returns_newest(tmp_path: Path) -> None:
    """OpenCode 1.18.10 GET /session/:id/message?limit=N is the newest page.

    OSM always sends limit=400. An oldest-first page would drop this turn on
    a long resumed ses_* and assess_idle could ship the previous stop.
    """
    from tests.test_live_true_positive_fixes import LiveServe

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not on PATH")

    cwd = tmp_path / "ws"
    cwd.mkdir()
    (cwd / "README.md").write_text("limit probe\n", encoding="utf-8")
    serve = LiveServe(cwd, tmp_path / "serve.log")
    try:
        serve.wait_ready()
        provider, model = serve.pick_model()
        sid = serve.create("limit-order")
        serve.prompt(
            sid,
            "Reply with exactly the word FIRST and nothing else. Do not use tools.",
            provider=provider,
            model=model,
        )
        first = serve.wait_idle(sid, timeout=180)
        assert first, "first turn produced no messages"
        serve.prompt(
            sid,
            "Reply with exactly the word SECOND and nothing else. Do not use tools.",
            provider=provider,
            model=model,
        )
        both = serve.wait_idle(sid, timeout=180)
        assert len(both) >= 2, both
        limited = serve.client.http.get(
            f"/session/{sid}/message",
            params={"limit": 1},
            headers=serve.client.headers,
            timeout=30.0,
        )
        assert limited.status_code == 200, limited.text[:400]
        page = limited.json()
        assert isinstance(page, list) and page, page
        blob = json.dumps(page).lower()
        assert "second" in blob or page[-1].get("id") == both[-1].get("id") or (
            (page[-1].get("info") or {}).get("id") == (both[-1].get("info") or {}).get("id")
        ), (
            "limit=1 did not return the newest message; long sessions would "
            f"lose this turn. page={page!r} both_last={both[-1]!r}"
        )
    finally:
        serve.close()


@pytest.mark.live
def test_live_review_wait_idle_does_not_return_previous_turn(tmp_path: Path) -> None:
    """Real /ask follow-up: review wait_idle must return this turn, not the last review."""
    from tests.test_live_true_positive_fixes import LiveServe

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not on PATH")

    cwd = tmp_path / "ws"
    cwd.mkdir()
    (cwd / "README.md").write_text("ask follow-up\n", encoding="utf-8")
    serve = LiveServe(cwd, tmp_path / "serve.log")
    review = None
    try:
        serve.wait_ready()
        provider, model = serve.pick_model()
        sid = serve.create("ask-followup")
        serve.prompt(
            sid,
            "Reply with exactly the token PREV-REVIEW-TOKEN and nothing else. Do not use tools.",
            provider=provider,
            model=model,
        )
        serve.wait_idle(sid, timeout=180)
        review = OpenCodeClient(serve.base, str(cwd))
        review.post_message(
            sid,
            "Reply with exactly the token THIS-ASK-TOKEN and nothing else. Do not use tools.",
            model=f"{provider}/{model}",
            agent="orchestrator",
        )
        text = review.wait_idle(
            sid,
            timeout=180.0,
            hang_timeout=120.0,
            idle_settle=8.0,
        )
        assert "THIS-ASK-TOKEN" in text, (
            "review wait_idle returned the previous turn as this /ask product: "
            f"{text!r}"
        )
        assert "PREV-REVIEW-TOKEN" not in text
    finally:
        if review is not None:
            review.close()
        serve.close()
