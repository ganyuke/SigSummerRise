from __future__ import annotations

import pytest

from sigsummerrise.config import Settings
from sigsummerrise.db import Database


@pytest.fixture
def tmp_db(tmp_path):
    path = str(tmp_path / "test.db")
    db = Database(path, "unit-test-sqlcipher-key")
    db.init()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def load_responses():
    from sigsummerrise.responses import init_responses, reset_responses

    init_responses("copy/responses.example.json")
    yield
    reset_responses()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "test.db"),
        db_key="unit-test-sqlcipher-key",
        public_base_url="http://testserver",
        signal_group_id="abc123",
        signal_bot_aci="11111111-1111-1111-1111-111111111111",
        max_n=200,
        dashboard_links_per_hour=3,
        magic_token_ttl_seconds=900,
        session_ttl_seconds=86400,
        openrouter_api_key="",
        llm_calls_per_hour=10,
        responses_path="copy/responses.example.json",
    )
