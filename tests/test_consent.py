from sigsummerrise.consent import CONSENT_COOLDOWN_SECONDS, pick_group_roast, should_send_consent_dm
from sigsummerrise.responses import get_responses


def test_first_contact_sends():
    assert should_send_consent_dm("unknown", None, 1000) is True


def test_opted_in_never_sends():
    assert should_send_consent_dm("opted_in", 1, 10**12) is False


def test_cooldown():
    now = CONSENT_COOLDOWN_SECONDS + 50
    assert should_send_consent_dm("declined", 40, now) is True
    assert should_send_consent_dm("unknown", now - 10, now) is False


def test_roast_pool():
    roasts = get_responses().group_roasts
    assert len(roasts) >= 5
    assert pick_group_roast() in roasts
