import json
from pathlib import Path

import pytest

from sigsummerrise.responses import init_responses, load_responses, reset_responses


def test_load_example_responses():
    bundle = load_responses("copy/responses.example.json")
    assert bundle.group_roast_chance == 0.25
    assert len(bundle.group_roasts) >= 5
    assert "{count}" in bundle.status_template


def test_missing_file_falls_back_to_example(tmp_path):
    missing = tmp_path / "responses.json"
    example = tmp_path / "responses.example.json"
    example.write_text(
        json.dumps(
            {
                "group_roast_chance": 0.1,
                "consent_dm": "dm",
                "consent_clarify": "clarify",
                "opted_in": "in",
                "declined": "declined",
                "opted_out": "out",
                "unopted_group_notice": "not opted in",
                "group_roasts": ["roast"],
                "help_text": "help",
                "unknown_replies": ["unknown"],
                "llm_fail": "fail",
                "llm_rate_replies": ["rate limited"],
                "summarize_in_dm": "summarize",
                "empty_window": "empty",
                "dashboard_rate": "dash rate",
                "dashboard_group": "dash group",
                "dashboard_dm": "dash {url}",
                "status_template": "{count} {when}",
            }
        ),
        encoding="utf-8",
    )
    bundle = load_responses(str(missing))
    assert bundle.consent_dm == "dm"


def test_init_requires_reset_between_tests():
    reset_responses()
    init_responses("copy/responses.example.json")
    assert load_responses("copy/responses.example.json").help_text
