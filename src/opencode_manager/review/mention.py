"""Detect @mentions of the review bot in GitLab notes and Azure comments."""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

_GUID = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_VSS_MENTION = re.compile(
    rf'data-vss-mention\s*=\s*["\'][^"\']*?({_GUID})',
    re.IGNORECASE,
)
# TFS mention picker often inserts "@<VSID>" as plain text, not HTML.
_AT_GUID = re.compile(rf"@<({_GUID})>", re.IGNORECASE)
_HTML_MENTION = re.compile(r"<a\b[^>]*data-vss-mention[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_HTML_INNER = re.compile(
    r"<a\b[^>]*data-vss-mention[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,!?:;])")
# Azure mention picker often emits "</a>/ask" with no space. Typed
# "@name/ask" has no space either. HTML tags are stripped to spaces
# before this runs, so "</a>/ask" becomes " /ask".
_CMD_RE = re.compile(
    r"(?:^|\s|@[^\s@/]+)/(ask|review)(?=[\s\"'.,!?:;)]|$)",
    re.IGNORECASE,
)
_YAVER_RE = re.compile(
    r"(?:^|\s|@[^\s@/]+)/yaver(?=[\s\"'.,!?:;)]|$)",
    re.IGNORECASE,
)
_CMD_TRAIL = ".,!?:;)"
_AT_HANDLE = re.compile(
    r"(?<![A-Za-z0-9._-])@("
    r"(?:[^\s@<>/]+\\)?[^\s@<>/]+"
    r"(?:\s+[A-Za-z][A-Za-z0-9.'-]*){0,4}"
    r")"
)


def parse_mention_aliases(raw: str) -> list[str]:
    names: list[str] = []
    for part in str(raw or "").split(","):
        name = part.strip().lstrip("@")
        if name:
            names.append(name)
    return names


def collect_names(*groups: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for raw in group:
            text = str(raw or "").strip().lstrip("@")
            if not text:
                continue
            if text not in seen:
                seen.append(text)
            if "\\" in text:
                tail = text.rsplit("\\", 1)[-1].strip()
                if tail and tail not in seen:
                    seen.append(tail)
    return seen


def azure_mention_ids(text: str) -> list[str]:
    found: list[str] = []
    for regex in (_VSS_MENTION, _AT_GUID):
        for match in regex.finditer(text or ""):
            guid = match.group(1)
            if guid not in found:
                found.append(guid)
    return found


def _plain_comment(text: str) -> str:
    return _HTML_TAG.sub(" ", text or "")


def plain_comment(text: str) -> str:
    """HTML-stripped comment or description text for the model prompt."""
    lines = []
    for line in _plain_comment(text).splitlines():
        cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(line.split()))
        lines.append(cleaned)
    return "\n".join(lines).strip()


def extract_mentioned_names(text: str) -> list[str]:
    """@handles and Azure mention-link labels from a comment."""
    raw = text or ""
    found: list[str] = []

    def add(name: str) -> None:
        cleaned = str(name or "").strip().strip("<>").lstrip("@").strip()
        if cleaned and cleaned not in found:
            found.append(cleaned)

    for match in _HTML_INNER.finditer(raw):
        add(" ".join(_plain_comment(match.group(1)).split()))
    for match in _AT_GUID.finditer(raw):
        add(match.group(1))
    for match in _AT_HANDLE.finditer(_plain_comment(raw)):
        add(match.group(1))
    return found


def has_yaver_command(body: str) -> bool:
    """True for `@mention /yaver`. That pattern is ignored (no usage note)."""
    return bool(_YAVER_RE.search(_plain_comment(body or "")))


def first_slash_command(body: str) -> Optional[tuple[str, str]]:
    """Return (command, remainder) for the first /ask or /review token."""
    text = _plain_comment(body or "")
    match = _CMD_RE.search(text)
    if not match:
        return None
    command = match.group(1).lower()
    remainder = text[match.end() :].lstrip(_CMD_TRAIL).strip()
    return command, remainder


def has_bot_mention(
    body: str,
    names: Sequence[str],
    *,
    mentioned_ids: Sequence[str] = (),
    bot_id: str = "",
    extra_ids: Sequence[str] = (),
) -> bool:
    text = body or ""
    known = {str(bot_id or "").strip().lower()}
    known.update(str(item or "").strip().lower() for item in extra_ids)
    known.discard("")
    ids = [*(mentioned_ids or ()), *azure_mention_ids(text)]
    if known and any(str(item or "").strip().lower() in known for item in ids):
        return True
    aliases = collect_names(names)
    if _mention_match(text, aliases) is not None:
        return True
    if _mention_match(_plain_comment(text), aliases) is not None:
        return True
    mentioned = {item.lower() for item in collect_names(extract_mentioned_names(text))}
    known_names = {item.lower() for item in aliases}
    return bool(known_names and mentioned.intersection(known_names))


def _mention_match(text: str, names: Sequence[str]) -> Optional[re.Match[str]]:
    aliases = collect_names(names)
    aliases.sort(key=len, reverse=True)
    for alias in aliases:
        pattern = re.escape(alias).replace(r"\ ", r"\s+")
        match = re.search(
            rf"(?<![A-Za-z0-9._-])@(?:[^\s@]+\\)?{pattern}(?![A-Za-z0-9._-])",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match
    return None


def strip_bot_mentions(text: str, names: Sequence[str]) -> str:
    cleaned = _HTML_MENTION.sub(" ", text or "")
    cleaned = _AT_GUID.sub(" ", cleaned)
    while True:
        match = _mention_match(cleaned, names)
        if not match:
            break
        cleaned = f"{cleaned[: match.start()]} {cleaned[match.end() :]}"
    return " ".join(cleaned.split()).strip(".,;:")


def user_comment_text(body: str, names: Sequence[str]) -> str:
    """User words with the bot mention and slash command removed."""
    text = " ".join(_plain_comment(strip_bot_mentions(body or "", names)).split())
    parsed = first_slash_command(text)
    if parsed is None:
        return text
    match = _CMD_RE.search(text)
    if not match:
        return parsed[1]
    leftover = f"{text[: match.start()]} {text[match.end() :].lstrip(_CMD_TRAIL)}"
    return " ".join(leftover.split()).strip()


def comment_intent(
    body: str,
    names: Sequence[str],
    *,
    mentioned_ids: Sequence[str] = (),
    bot_id: str = "",
    extra_ids: Sequence[str] = (),
) -> Optional[tuple[str, str, str]]:
    """Parse a comment.

    Returns ``("run", "ask"|"review", remainder)`` when the bot is
    mentioned and ``/ask`` or ``/review`` is present.
    Returns ``("usage", "usage", leftover)`` when the bot is mentioned
    without those commands. ``@mention /yaver`` is ignored (``None``).
    Otherwise ``None``.
    """
    mentioned = has_bot_mention(
        body,
        names,
        mentioned_ids=mentioned_ids,
        bot_id=bot_id,
        extra_ids=extra_ids,
    )
    parsed = first_slash_command(body)
    leftover = user_comment_text(body, names)
    if not mentioned:
        return None
    if not parsed:
        if has_yaver_command(body):
            return None
        return "usage", "usage", leftover
    command, remainder = parsed
    remainder = strip_bot_mentions(remainder, names)
    if command == "ask":
        from opencode_manager.review.ask import ask_wants_new_review

        if ask_wants_new_review(leftover or remainder):
            command = "review"
    return "run", command, remainder


USAGE_HEADING = "**aMIR-mini — how to run a command**"
USAGE_MARKER = "<!-- amir-mini-usage -->"
_LEGACY_USAGE_HEADINGS = (
    "**Creasy — how to run a command**",
    "**MIReviewer — how to run a command**",
)
_LEGACY_USAGE_MARKER = "<!-- creasy-usage -->"


def is_usage_note(body: str) -> bool:
    """True for a help note, so its examples do not start a job."""
    text = body or ""
    return (
        USAGE_MARKER in text
        or USAGE_HEADING in text
        or _LEGACY_USAGE_MARKER in text
        or any(heading in text for heading in _LEGACY_USAGE_HEADINGS)
    )
