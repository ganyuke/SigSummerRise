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


def test_ask_intent():
    intent = parse_intent("@grok hello there", mentioned=True, in_dm=False, max_n=200)
    assert intent.name == "ask"


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


def test_casual_mentions_are_not_commands():
    cases = (
        "I had to opt out of my gym membership",
        "what's the status of the project",
        "can you help me with homework",
        "login to the website from my phone",
        "check my stats for the week",
        "don't summarize the past 50 messages in your head",
    )
    for text in cases:
        intent = parse_intent(text, mentioned=True, in_dm=False, max_n=200)
        assert intent.name == "ask", text


def test_opt_out_confirm_phrase():
    from sigsummerrise.commands import matches_opt_out_confirm

    assert matches_opt_out_confirm("Please forget me, SigSummerRise", "SigSummerRise")
    assert matches_opt_out_confirm("Please forget me, SigSummerRise!", "SigSummerRise")
    assert not matches_opt_out_confirm("Please forget me, OtherBot", "SigSummerRise")
