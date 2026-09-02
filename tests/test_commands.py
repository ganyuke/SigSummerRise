from sigsummerrise.commands import help_text, parse_intent, pick_unknown_reply
from sigsummerrise.responses import get_responses


def test_summarize_not_dashboard():
    intent = parse_intent(
        "@grok summarize the past 50 messages",
        mentioned=True,
        in_dm=False,
        max_n=200,
    )
    assert intent.name == "summarize"
    assert intent.n == 50


def test_summarize_caps_max_n():
    intent = parse_intent(
        "summarize the last 999 messages",
        mentioned=True,
        in_dm=False,
        max_n=200,
    )
    assert intent.name == "summarize"
    assert intent.n == 200


def test_dashboard_keywords():
    for text in ("dashboard", "website", "login", "magic link", "my stats"):
        intent = parse_intent(text, mentioned=True, in_dm=False, max_n=200)
        assert intent.name == "dashboard", text


def test_opt_out():
    intent = parse_intent("@grok please opt out", mentioned=True, in_dm=False, max_n=200)
    assert intent.name == "opt_out"
    intent = parse_intent("stop collecting", mentioned=True, in_dm=False, max_n=200)
    assert intent.name == "opt_out"


def test_status():
    intent = parse_intent("status", mentioned=True, in_dm=False, max_n=200)
    assert intent.name == "status"


def test_unknown_is_unknown():
    intent = parse_intent("@grok hello there", mentioned=True, in_dm=False, max_n=200)
    assert intent.name == "unknown"


def test_help_keyword():
    assert parse_intent("help", mentioned=True, in_dm=False, max_n=200).name == "help"
    assert parse_intent("commands", mentioned=True, in_dm=False, max_n=200).name == "help"
    assert pick_unknown_reply() in get_responses().unknown_replies


def test_unmentioned_group_is_none():
    intent = parse_intent("dashboard", mentioned=False, in_dm=False, max_n=200)
    assert intent.name == "none"


def test_dm_yes_no():
    assert parse_intent("Yes", mentioned=True, in_dm=True, max_n=200).name == "yes"
    assert parse_intent("n", mentioned=True, in_dm=True, max_n=200).name == "no"


def test_dm_dashboard_without_mention():
    intent = parse_intent("dashboard", mentioned=True, in_dm=True, max_n=200)
    assert intent.name == "dashboard"
