from __future__ import annotations

import logging

import httpx

from sigsummerrise.config import Settings

log = logging.getLogger("sigsummerrise.llm")

SUMMARIZE_SYSTEM = (
    "You summarize a Signal group chat for the people in it. "
    "Lines that say [redacted] were not consented for this use; "
    "do not guess who wrote them or what they said. "
    "Reply with a concise summary only."
)

FOLLOWUP_SYSTEM = (
    "You answer a follow-up question about a Signal group chat summary. "
    "Lines that say [redacted] were not consented for this use; "
    "do not guess who wrote them or what they said. "
    "Reply with a concise answer only."
)


class LlmError(Exception):
    pass


async def complete(settings: Settings, system: str, user: str) -> str:
    if not settings.openrouter_api_key:
        raise LlmError("not configured")
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "provider": {"zdr": True, "data_collection": "deny"},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": settings.public_origin,
        "X-Title": "SigSummerRise",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.HTTPError:
        log.exception("OpenRouter request failed")
        raise LlmError("unavailable") from None
    if response.status_code >= 400:
        log.error("OpenRouter HTTP %s", response.status_code)
        raise LlmError("unavailable")
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        log.error("OpenRouter response missing content")
        raise LlmError("unavailable") from None
    if not isinstance(content, str) or not content.strip():
        raise LlmError("unavailable")
    return content.strip()
