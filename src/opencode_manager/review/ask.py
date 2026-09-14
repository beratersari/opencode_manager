"""Detect when an /ask question is actually a request for a new review."""

from __future__ import annotations

import re

_ASK_WANTS_REVIEW = re.compile(
    r"(?i)("
    r"\bnew review\b|"
    r"\bfull review\b|"
    r"\banother review\b|"
    r"\bre-?review\b|"
    r"\breview again\b|"
    r"\bstart a review\b|"
    r"\brun a (?:new |full )review\b|"
    r"\bdo a (?:new |full )review\b|"
    r"yeni (?:bir )?review\b|"
    r"yeni inceleme\b|"
    r"tekrar review\b|"
    r"yeniden incele(?:me)?\b"
    r")"
)


def ask_wants_new_review(text: str) -> bool:
    """True only when the /ask text explicitly asks for another review."""
    return bool(_ASK_WANTS_REVIEW.search(text or ""))
