from __future__ import annotations

from opencode_manager.review.mention import (
    USAGE_MARKER,
    azure_mention_ids,
    collect_names,
    comment_intent,
    extract_mentioned_names,
    is_usage_note,
    parse_mention_aliases,
    plain_comment,
)


def test_plain_comment_strips_html_keeps_words() -> None:
    html = (
        '<div>Unbounded strcpy into dest.</div>'
        '<a href="#" data-vss-mention="version:2.0,aaaa">@Creasy</a>'
    )
    assert plain_comment(html) == "Unbounded strcpy into dest. @Creasy"
    assert plain_comment("<p>Watch <b>dest</b>.</p>") == "Watch dest."
    assert plain_comment("  already   plain  ") == "already plain"


def test_parse_and_collect_names() -> None:
    assert parse_mention_aliases(" @creasy, Creasy Bot,") == ["creasy", "Creasy Bot"]
    names = collect_names(["DOMAIN\\creasy"], ["Creasy"])
    assert "DOMAIN\\creasy" in names
    assert "creasy" in names
    assert "Creasy" in names


def test_comment_intent_requires_mention_and_command() -> None:
    assert comment_intent("ping creasy@company.com", ["creasy"]) is None
    assert comment_intent("@other please", ["creasy"]) is None
    assert comment_intent("@creasy-bot", ["creasy"]) is None
    assert comment_intent("hey @creasy check auth", ["creasy"]) == (
        "usage",
        "usage",
        "hey check auth",
    )
    assert comment_intent("/review focus", ["creasy"]) is None
    leftover = comment_intent("hey @creasy /review check auth", ["creasy"])
    assert leftover == ("run", "review", "check auth")
    promoted = comment_intent("@creasy /ask please do a new review", ["creasy"])
    assert promoted == ("run", "review", "please do a new review")
    assert comment_intent("/ask why", ["creasy"]) is None
    ask = comment_intent("/ask why @creasy", ["creasy"])
    assert ask == ("run", "ask", "why")
    domain = comment_intent(r"@company\mberatersari /ask asdfasf", ["mberatersari"])
    assert domain == ("run", "ask", "asdfasf")


def test_extract_mentioned_names_from_plain_and_html() -> None:
    assert extract_mentioned_names('@mberatersari /ask "why"') == ["mberatersari"]
    assert extract_mentioned_names(r"@company\mberatersari /ask why") == [r"company\mberatersari"]
    html = (
        '<a href="#" data-vss-mention="version:2.0,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">'
        '@Berat ERSARI</a>/ask "why this lock?"'
    )
    assert extract_mentioned_names(html) == ["Berat ERSARI"]


def test_comment_intent_ask_without_space_after_mention() -> None:
    assert comment_intent("@creasy/ask why", ["creasy"]) == ("run", "ask", "why")
    quoted = comment_intent('@creasy /ask "question"', ["creasy"])
    assert quoted == ("run", "ask", '"question"')
    html = (
        '<a href="#" data-vss-mention="version:2.0,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">'
        '@Berat ERSARI</a>/ask "why this lock?"'
    )
    got = comment_intent(html, ["Berat ERSARI"])
    assert got == ("run", "ask", '"why this lock?"')
    display = comment_intent("@Berat ERSARI /ask why", ["mberatersari", "Berat ERSARI"])
    assert display == ("run", "ask", "why")


def test_tfs_angle_guid_mention_starts_ask() -> None:
    text = "@<71440E05-BE9E-4768-897E-DA81A889D26E> /ask hey buradaki sorun ne"
    assert azure_mention_ids(text) == ["71440E05-BE9E-4768-897E-DA81A889D26E"]
    assert extract_mentioned_names(text) == ["71440E05-BE9E-4768-897E-DA81A889D26E"]
    got = comment_intent(
        text,
        ["mberatersari"],
        extra_ids=["71440e05-be9e-4768-897e-da81a889d26e"],
    )
    assert got == ("run", "ask", "hey buradaki sorun ne")
    assert comment_intent(text, ["mberatersari"]) is None


def test_azure_html_mention_needs_command() -> None:
    html = '<a href="#" data-vss-mention="version:2.0,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">@X</a>'
    assert azure_mention_ids(html) == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    ids = azure_mention_ids(html)
    assert comment_intent(html, [], mentioned_ids=ids, bot_id="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE") == (
        "usage",
        "usage",
        "",
    )
    paired = comment_intent(
        html + " /ask why",
        [],
        mentioned_ids=ids,
        bot_id="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
    )
    assert paired == ("run", "ask", "why")


def test_old_usage_note_marker_is_detected() -> None:
    text = f"{USAGE_MARKER}\n@creasy /ask why is this lock held?"
    assert is_usage_note(text) is True
    assert is_usage_note("prefix\n" + text) is True
    assert is_usage_note("@creasy /ask why") is False
