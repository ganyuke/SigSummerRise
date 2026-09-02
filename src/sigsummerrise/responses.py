from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONSENT_COOLDOWN_SECONDS = 24 * 60 * 60

_REQUIRED_STRINGS = (
    "consent_dm",
    "consent_clarify",
    "opted_in",
    "declined",
    "opted_out",
    "unopted_group_notice",
    "help_text",
    "llm_fail",
    "summarize_in_dm",
    "empty_window",
    "dashboard_rate",
    "dashboard_group",
    "dashboard_dm",
    "status_template",
)
_REQUIRED_LISTS = ("group_roasts", "unknown_replies", "llm_rate_replies")


@dataclass(frozen=True)
class Responses:
    group_roast_chance: float
    consent_dm: str
    consent_clarify: str
    opted_in: str
    declined: str
    opted_out: str
    unopted_group_notice: str
    group_roasts: tuple[str, ...]
    help_text: str
    unknown_replies: tuple[str, ...]
    llm_fail: str
    llm_rate_replies: tuple[str, ...]
    summarize_in_dm: str
    empty_window: str
    dashboard_rate: str
    dashboard_group: str
    dashboard_dm: str
    status_template: str

    def format_status(self, count: int, when: str) -> str:
        return self.status_template.format(count=count, when=when)

    def pick_group_roast(self) -> str:
        return random.choice(self.group_roasts)

    def pick_unknown_reply(self) -> str:
        return random.choice(self.unknown_replies)

    def pick_llm_rate_reply(self) -> str:
        return random.choice(self.llm_rate_replies)

    def should_roast_unopted_mention(self) -> bool:
        return random.random() < self.group_roast_chance


_bundle: Responses | None = None


def _repo_copy_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "copy"


def resolve_responses_path(path: str) -> Path:
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
    example = configured.parent / "responses.example.json"
    if example.is_file():
        return example
    repo_example = _repo_copy_dir() / "responses.example.json"
    if repo_example.is_file():
        return repo_example
    raise FileNotFoundError(
        f"responses file not found: {path} (also tried {example} and {repo_example})"
    )


def _require_nonempty_strings(data: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{key} must be a non-empty list of strings")
    values = tuple(str(item).strip() for item in raw)
    if not all(values):
        raise ValueError(f"{key} entries must be non-empty strings")
    return values


def _example_defaults() -> dict[str, Any]:
    path = _repo_copy_dir() / "responses.example.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("responses.example.json must be a JSON object")
    return raw


def _merge_responses_data(data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing keys from responses.example.json so operator copy can lag behind code."""
    merged = dict(_example_defaults())
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                merged[key] = value
        elif isinstance(value, list):
            if value:
                merged[key] = value
        else:
            merged[key] = value
    if "llm_rate_replies" not in data or not data.get("llm_rate_replies"):
        legacy = data.get("llm_rate")
        if isinstance(legacy, str) and legacy.strip():
            merged["llm_rate_replies"] = [legacy.strip()]
    return merged


def load_responses(path: str) -> Responses:
    resolved = resolve_responses_path(path)
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("responses file must be a JSON object")
    data = _merge_responses_data(raw)
    for key in _REQUIRED_STRINGS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    for key in _REQUIRED_LISTS:
        _require_nonempty_strings(data, key)
    chance = data.get("group_roast_chance", 0.25)
    if not isinstance(chance, (int, float)) or not 0 <= chance <= 1:
        raise ValueError("group_roast_chance must be a number between 0 and 1")
    return Responses(
        group_roast_chance=float(chance),
        consent_dm=data["consent_dm"].strip(),
        consent_clarify=data["consent_clarify"].strip(),
        opted_in=data["opted_in"].strip(),
        declined=data["declined"].strip(),
        opted_out=data["opted_out"].strip(),
        unopted_group_notice=data["unopted_group_notice"].strip(),
        group_roasts=_require_nonempty_strings(data, "group_roasts"),
        help_text=data["help_text"].strip(),
        unknown_replies=_require_nonempty_strings(data, "unknown_replies"),
        llm_fail=data["llm_fail"].strip(),
        llm_rate_replies=_require_nonempty_strings(data, "llm_rate_replies"),
        summarize_in_dm=data["summarize_in_dm"].strip(),
        empty_window=data["empty_window"].strip(),
        dashboard_rate=data["dashboard_rate"].strip(),
        dashboard_group=data["dashboard_group"].strip(),
        dashboard_dm=data["dashboard_dm"].strip(),
        status_template=data["status_template"].strip(),
    )


def init_responses(path: str) -> Responses:
    global _bundle
    _bundle = load_responses(path)
    return _bundle


def get_responses() -> Responses:
    if _bundle is None:
        raise RuntimeError("responses not initialized; call init_responses() first")
    return _bundle


def reset_responses() -> None:
    global _bundle
    _bundle = None
