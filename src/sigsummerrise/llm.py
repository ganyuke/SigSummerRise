from __future__ import annotations

import logging
from typing import Any

import httpx

from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.runtime import ResolvedLlmConfig, resolve_llm_config

log = logging.getLogger("sigsummerrise.llm")


class LlmError(Exception):
    pass


def _extract_usage(data: dict[str, Any]) -> tuple[int | None, int | None, float | None]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    cost = usage.get("cost")
    if cost is None:
        cost = usage.get("total_cost")
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None
    try:
        prompt_i = int(prompt) if prompt is not None else None
    except (TypeError, ValueError):
        prompt_i = None
    try:
        completion_i = int(completion) if completion is not None else None
    except (TypeError, ValueError):
        completion_i = None
    return prompt_i, completion_i, cost_f


async def complete(
    settings: Settings,
    db: Database,
    system: str,
    user: str,
    *,
    issuance_id: int | None = None,
    config: ResolvedLlmConfig | None = None,
) -> str:
    cfg = config or resolve_llm_config(settings, db)
    if not cfg.openrouter_api_key:
        raise LlmError("not configured")
    payload: dict[str, Any] = {
        "model": cfg.openrouter_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "provider": {"zdr": True, "data_collection": "deny"},
        "usage": {"include": True},
    }
    if cfg.llm_temperature is not None:
        payload["temperature"] = cfg.llm_temperature
    if cfg.llm_max_tokens is not None:
        payload["max_tokens"] = cfg.llm_max_tokens
    headers = {
        "Authorization": f"Bearer {cfg.openrouter_api_key}",
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
    if issuance_id is not None:
        prompt_t, completion_t, cost = _extract_usage(data)
        db.update_llm_usage(
            issuance_id,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            cost_usd=cost,
        )
    if not isinstance(content, str) or not content.strip():
        raise LlmError("unavailable")
    return content.strip()
