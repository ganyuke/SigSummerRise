import json

import pytest

from sigsummerrise.prompts import (
    format_current_time,
    init_prompts,
    load_prompts,
    render_system_prompt,
    reset_prompts,
)


def test_load_example_prompts():
    bundle = load_prompts("copy/prompts.example.json")
    assert "[redacted]" in bundle.summarize_system
    assert "follow-up" in bundle.followup_system.lower()
    assert "@mention" in bundle.ask_system


def test_missing_file_falls_back_to_example(tmp_path):
    missing = tmp_path / "prompts.json"
    example = tmp_path / "prompts.example.json"
    example.write_text(
        json.dumps(
            {
                "summarize_system": "custom summarize",
                "followup_system": "custom followup",
                "ask_system": "custom ask",
            }
        ),
        encoding="utf-8",
    )
    bundle = load_prompts(str(missing))
    assert bundle.summarize_system == "custom summarize"


def test_missing_keys_fall_back_to_example(tmp_path):
    partial = tmp_path / "prompts.json"
    partial.write_text(
        json.dumps({"summarize_system": "only summarize"}),
        encoding="utf-8",
    )
    bundle = load_prompts(str(partial))
    assert bundle.summarize_system == "only summarize"
    assert "follow-up" in bundle.followup_system.lower()
    assert "@mention" in bundle.ask_system


def test_render_system_prompt_substitutes_name_and_time():
    rendered = render_system_prompt(
        "Hi {bot_name} in {group_name}, it is {current_time}.",
        bot_name="TestBot",
        current_time="Wednesday, 2026-09-03 12:00 UTC",
        group_name="μ's",
    )
    assert rendered == "Hi TestBot in μ's, it is Wednesday, 2026-09-03 12:00 UTC."


def test_render_system_prompt_defaults_group_name():
    rendered = render_system_prompt(
        "Group: {group_name}",
        bot_name="TestBot",
        current_time="now",
    )
    assert rendered == "Group: this group"


def test_format_current_time_uses_timezone():
    # 2023-11-14 22:13 UTC
    assert format_current_time(1_700_000_000, "UTC") == "Tuesday, 2023-11-14 22:13 UTC"


def test_format_current_time_invalid_timezone_falls_back_to_utc():
    assert format_current_time(1_700_000_000, "Not/AZone").endswith("UTC")


def test_init_requires_reset_between_tests():
    reset_prompts()
    init_prompts("copy/prompts.example.json")
    assert load_prompts("copy/prompts.example.json").ask_system
