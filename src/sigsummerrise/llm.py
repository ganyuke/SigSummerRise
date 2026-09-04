from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.runtime import ResolvedLlmConfig, resolve_llm_config

log = logging.getLogger("sigsummerrise.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LlmError(Exception):
    pass


class LlmTimeoutError(LlmError):
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


def _build_provider_payload(cfg: ResolvedLlmConfig) -> dict[str, Any]:
    provider: dict[str, Any] = {"zdr": True, "data_collection": "deny"}
    if cfg.provider_order:
        provider["order"] = list(cfg.provider_order)
    if cfg.provider_ignore:
        provider["ignore"] = list(cfg.provider_ignore)
    if cfg.provider_sort:
        provider["sort"] = cfg.provider_sort
    return provider


def _parse_stream_line(line: str) -> tuple[str, dict[str, Any] | None]:
    text = line.strip()
    if not text or text == "data: [DONE]":
        return "", None
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return "", None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "", None
    if not isinstance(payload, dict):
        return "", None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", payload
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", payload
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str) and content:
            return content, payload
    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content:
            return content, payload
    return "", payload


async def _stream_completion(
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
    on_chunk: Callable[[str], None] | None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    last_payload: dict[str, Any] = {}
    async with client.stream("POST", OPENROUTER_URL, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = await response.aread()
            log.error("OpenRouter HTTP %s: %s", response.status_code, body[:200])
            raise LlmError("unavailable")
        async for line in response.aiter_lines():
            chunk, meta = _parse_stream_line(line)
            if meta:
                last_payload = meta
            if chunk:
                parts.append(chunk)
                if on_chunk is not None:
                    on_chunk(chunk)
    return "".join(parts), last_payload


async def complete(
    settings: Settings,
    db: Database,
    system: str,
    user: str,
    *,
    issuance_id: int | None = None,
    config: ResolvedLlmConfig | None = None,
    on_chunk: Callable[[str], None] | None = None,
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
        "provider": _build_provider_payload(cfg),
        "usage": {"include": True},
        "stream": True,
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
    timeout = httpx.Timeout(
        connect=30.0,
        read=float(cfg.llm_read_idle_seconds),
        write=30.0,
        pool=30.0,
    )
    started = time.monotonic()
    outcome = "error"
    response_model: str | None = None
    response_provider: str | None = None
    prompt_t: int | None = None
    completion_t: int | None = None
    cost: float | None = None
    content = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            content, last_payload = await asyncio.wait_for(
                _stream_completion(
                    client=client,
                    headers=headers,
                    payload=payload,
                    on_chunk=on_chunk,
                ),
                timeout=float(cfg.llm_timeout_seconds),
            )
        if not content.strip():
            raise LlmError("unavailable")
        response_model = str(last_payload.get("model") or cfg.openrouter_model)
        provider_raw = last_payload.get("provider")
        if isinstance(provider_raw, str) and provider_raw.strip():
            response_provider = provider_raw.strip()
        prompt_t, completion_t, cost = _extract_usage(last_payload)
        outcome = "ok"
        return content.strip()
    except asyncio.TimeoutError:
        outcome = "timeout"
        log.error("OpenRouter request timed out after %ss", cfg.llm_timeout_seconds)
        raise LlmTimeoutError("timeout") from None
    except httpx.HTTPError:
        log.exception("OpenRouter request failed")
        raise LlmError("unavailable") from None
    except LlmError:
        raise
    except Exception:
        log.exception("OpenRouter request failed")
        raise LlmError("unavailable") from None
    finally:
        if issuance_id is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            db.finalize_llm_call(
                issuance_id,
                latency_ms=latency_ms,
                model=response_model or cfg.openrouter_model,
                provider=response_provider,
                outcome=outcome,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                cost_usd=cost,
            )
