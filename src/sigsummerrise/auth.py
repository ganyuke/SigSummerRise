from __future__ import annotations

import secrets

from sigsummerrise.config import Settings
from sigsummerrise.db import Database


def new_token() -> str:
    return secrets.token_urlsafe(32)


def can_issue_link(count_last_hour: int, limit: int) -> bool:
    return count_last_hour < limit


def issue_magic_link(db: Database, settings: Settings, aci: str, now: int) -> str | None:
    if not can_issue_link(db.issuance_count(aci, now), settings.dashboard_links_per_hour):
        return None
    raw = new_token()
    expires_at = now + settings.magic_token_ttl_seconds
    db.create_magic_token(aci, raw, expires_at)
    db.record_issuance(aci, now)
    return f"{settings.public_origin}/a/{raw}"


def redeem_and_session(db: Database, settings: Settings, raw_token: str, now: int) -> str | None:
    aci = db.redeem_magic_token(raw_token, now)
    if aci is None:
        return None
    raw_session = new_token()
    db.create_session(aci, raw_session, now + settings.session_ttl_seconds)
    return raw_session


def cookie_kwargs(settings: Settings, value: str) -> dict:
    return {
        "key": settings.session_cookie_name,
        "value": value,
        "max_age": settings.session_ttl_seconds,
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "path": "/",
    }


def clear_session_cookie(response, settings: Settings) -> None:
    """Delete the session cookie with the same attributes used at login."""
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def revoke_session(request, db: Database, settings: Settings) -> None:
    raw = request.cookies.get(settings.session_cookie_name)
    if raw:
        db.delete_session(raw)
