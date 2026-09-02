from __future__ import annotations

from sigsummerrise.responses import CONSENT_COOLDOWN_SECONDS, get_responses


def should_send_consent_dm(consent_state: str, last_dm_at: int | None, now: int) -> bool:
    if consent_state == "opted_in":
        return False
    if last_dm_at is None:
        return True
    return now - last_dm_at >= CONSENT_COOLDOWN_SECONDS


def should_roast_unopted_mention() -> bool:
    return get_responses().should_roast_unopted_mention()


def pick_group_roast() -> str:
    return get_responses().pick_group_roast()
