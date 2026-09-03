from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REQUIRED_STRINGS = PROMPT_KEYS = ("summarize_system", "followup_system", "ask_system")


@dataclass(frozen=True)
class Prompts:
    summarize_system: str
    followup_system: str
    ask_system: str


_bundle: Prompts | None = None


def _repo_copy_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "copy"


def resolve_prompts_path(path: str) -> Path:
    configured = Path(path)
    if configured.is_file():
        return configured
    if not configured.is_absolute():
        from_cwd = Path.cwd() / configured
        if from_cwd.is_file():
            return from_cwd
        from_repo = _repo_copy_dir() / configured.name
        if from_repo.is_file():
            return from_repo
    example = configured.parent / "prompts.example.json"
    if example.is_file():
        return example
    repo_example = _repo_copy_dir() / "prompts.example.json"
    if repo_example.is_file():
        return repo_example
    raise FileNotFoundError(
        f"prompts file not found: {path} (also tried {example} and {repo_example})"
    )


def _example_defaults() -> dict[str, Any]:
    path = _repo_copy_dir() / "prompts.example.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prompts.example.json must be a JSON object")
    return raw


def _merge_prompts_data(data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(_example_defaults())
    for key, value in data.items():
        if isinstance(value, str) and value.strip():
            merged[key] = value
    return merged


def load_prompts(path: str) -> Prompts:
    resolved = resolve_prompts_path(path)
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prompts file must be a JSON object")
    data = _merge_prompts_data(raw)
    for key in _REQUIRED_STRINGS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    return Prompts(
        summarize_system=data["summarize_system"].strip(),
        followup_system=data["followup_system"].strip(),
        ask_system=data["ask_system"].strip(),
    )


def init_prompts(path: str) -> Prompts:
    global _bundle
    _bundle = load_prompts(path)
    return _bundle


def get_prompts() -> Prompts:
    if _bundle is None:
        raise RuntimeError("prompts not initialized; call init_prompts() first")
    return _bundle


def reset_prompts() -> None:
    global _bundle
    _bundle = None


def format_current_time(ts: int, tz_name: str = "UTC") -> str:
    try:
        tz = ZoneInfo(tz_name)
        label = tz_name
    except Exception:
        tz = timezone.utc
        label = "UTC"
    dt = datetime.fromtimestamp(ts, tz=tz)
    return dt.strftime(f"%A, %Y-%m-%d %H:%M {label}")


def render_system_prompt(
    template: str,
    *,
    bot_name: str,
    current_time: str,
    group_name: str = "",
) -> str:
    group = group_name.strip() or "this group"
    return (
        template.replace("{bot_name}", bot_name)
        .replace("{current_time}", current_time)
        .replace("{group_name}", group)
    )
