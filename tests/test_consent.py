from sigsummerrise.consent import (
    CONSENT_COOLDOWN_SECONDS,
    pick_group_roast,
    should_send_consent_dm,
    should_send_unopted_group_notice,
)
from sigsummerrise.responses import get_responses


def test_first_contact_sends():
    assert should_send_consent_dm("unknown", None, 1000) is True


def test_opted_in_never_sends():
    assert should_send_consent_dm("opted_in", 1, 10**12) is False


def test_cooldown():
    now = CONSENT_COOLDOWN_SECONDS + 50
    assert should_send_consent_dm("declined", 40, now) is True
    assert should_send_consent_dm("unknown", now - 10, now) is False


def test_unopted_group_notice_cooldown():
    now = CONSENT_COOLDOWN_SECONDS + 50
    assert should_send_unopted_group_notice(None, now) is True
    assert should_send_unopted_group_notice(40, now) is True
    assert should_send_unopted_group_notice(now - 10, now) is False


def test_roast_pool():
    roasts = get_responses().group_roasts
    assert len(roasts) >= 5
    assert pick_group_roast() in roasts


def test_llm_rate_pool():
    replies = get_responses().llm_rate_replies
    assert len(replies) >= 5
    assert get_responses().pick_llm_rate_reply() in replies
