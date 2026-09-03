from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.prompts import PROMPT_KEYS, Prompts, load_prompts


@dataclass(frozen=True)
class ResolvedLlmConfig:
    openrouter_api_key: str
    openrouter_model: str
    llm_temperature: float | None
    llm_max_tokens: int | None
    llm_calls_per_hour: int
    ask_context_n: int
    max_n: int
    api_key_configured: bool
    api_key_suffix: str | None


def _runtime_data(db: Database) -> dict[str, Any]:
    return db.get_runtime_config()


def resolve_llm_config(settings: Settings, db: Database) -> ResolvedLlmConfig:
    data = _runtime_data(db)
    env_key = (settings.openrouter_api_key or "").strip()
    db_key = str(data.get("openrouter_api_key") or "").strip()
    api_key = db_key or env_key
    suffix = None
    if api_key:
        suffix = api_key[-4:] if len(api_key) >= 4 else "****"
    temp = data.get("llm_temperature")
    max_tokens = data.get("llm_max_tokens")
    return ResolvedLlmConfig(
        openrouter_api_key=api_key,
        openrouter_model=str(data.get("openrouter_model") or settings.openrouter_model).strip(),
        llm_temperature=float(temp) if temp is not None and temp != "" else None,
        llm_max_tokens=int(max_tokens) if max_tokens is not None and max_tokens != "" else None,
        llm_calls_per_hour=int(data.get("llm_calls_per_hour") or settings.llm_calls_per_hour),
        ask_context_n=int(data.get("ask_context_n") or settings.ask_context_n),
        max_n=int(data.get("max_n") or settings.max_n),
        api_key_configured=bool(api_key),
        api_key_suffix=suffix,
    )


def resolve_prompts(settings: Settings, db: Database) -> Prompts:
    data = _runtime_data(db)
    base = load_prompts(settings.prompts_path)
    fields: dict[str, str] = {}
    for key in PROMPT_KEYS:
        override = data.get(key)
        if isinstance(override, str) and override.strip():
            fields[key] = override.strip()
        else:
            fields[key] = getattr(base, key)
    return Prompts(**fields)


def runtime_config_for_ops(settings: Settings, db: Database) -> dict[str, Any]:
    data = dict(_runtime_data(db))
    resolved = resolve_llm_config(settings, db)
    prompts = resolve_prompts(settings, db)
    return {
        "openrouter_model": resolved.openrouter_model,
        "llm_temperature": data.get("llm_temperature", ""),
        "llm_max_tokens": data.get("llm_max_tokens", ""),
        "llm_calls_per_hour": resolved.llm_calls_per_hour,
        "ask_context_n": resolved.ask_context_n,
        "max_n": resolved.max_n,
        "api_key_configured": resolved.api_key_configured,
        "api_key_suffix": resolved.api_key_suffix,
        "summarize_system": prompts.summarize_system,
        "followup_system": prompts.followup_system,
        "ask_system": prompts.ask_system,
        "prompts_from_db": any(data.get(k) for k in PROMPT_KEYS),
    }


def save_runtime_config(db: Database, patch: dict[str, Any], *, clear_prompts: bool = False) -> None:
    data = dict(_runtime_data(db))
    if clear_prompts:
        for key in PROMPT_KEYS:
            data.pop(key, None)
    for key, value in patch.items():
        if value is None:
            data.pop(key, None)
        elif key == "openrouter_api_key" and value == "":
            continue
        else:
            data[key] = value
    db.set_runtime_config(data)


def validate_prompt_fields(data: dict[str, Any]) -> None:
    for key in PROMPT_KEYS:
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
