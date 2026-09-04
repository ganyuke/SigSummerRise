import pytest

from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.main import require_runtime_settings


def test_duplicate_bodies_and_holes_are_ignored(tmp_db: Database):
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    first = tmp_db.insert_body(aci, 10, "hello")
    second = tmp_db.insert_body(aci, 10, "hello again")
    assert first > 0
    assert second == 0
    assert tmp_db.count_bodies(aci) == 1
    tmp_db.insert_hole(20)
    tmp_db.insert_hole(20)
    holes = tmp_db.connect().execute("SELECT COUNT(*) AS n FROM messages WHERE is_hole = 1").fetchone()
    assert holes["n"] == 1


def test_llm_issuance_counts(tmp_db: Database):
    aci = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    now = 1_900_000_000
    assert tmp_db.llm_count(aci, now) == 0
    tmp_db.record_llm_call(aci, now)
    tmp_db.record_llm_call(aci, now + 1)
    assert tmp_db.llm_count(aci, now + 2) == 2
    assert tmp_db.llm_count(aci, now + 3600 + 2) == 0


def test_llm_issuance_retained_beyond_one_hour(tmp_db: Database):
    aci = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    now = 1_900_000_000
    tmp_db.record_llm_call(aci, now)
    tmp_db.record_llm_call(aci, now - 7200)
    assert tmp_db.llm_calls_since(now, 7 * 86400) == 2


def test_require_runtime_settings():
    with pytest.raises(SystemExit, match="DB_KEY"):
        require_runtime_settings(Settings(db_key="", signal_group_id="g"))
    with pytest.raises(SystemExit, match="SIGNAL_GROUP_ID"):
        require_runtime_settings(Settings(db_key="k", signal_group_id=""))
    require_runtime_settings(Settings(db_key="k", signal_group_id="abc"))


def test_finalize_llm_call_and_provider_stats(tmp_db: Database):
    aci = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    now = 1_900_000_000
    row_id = tmp_db.record_llm_call(aci, now)
    tmp_db.finalize_llm_call(
        row_id,
        latency_ms=1200,
        model="test/model",
        provider="ProviderA",
        outcome="ok",
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.01,
    )
    assert tmp_db.last_llm_provider() == "ProviderA"
    count, median, p95 = tmp_db.llm_latency_stats("test/model", now)
    assert count == 1
    assert median == 1200
    assert p95 == 1200

    fail_id = tmp_db.record_llm_call(aci, now + 1)
    tmp_db.finalize_llm_call(
        fail_id,
        latency_ms=500,
        model="test/model",
        provider=None,
        outcome="timeout",
    )
    assert tmp_db.last_llm_provider() == "ProviderA"


def test_is_bot_message_ts(tmp_db: Database):
    bot_aci = "11111111-1111-1111-1111-111111111111"
    summary_id = tmp_db.save_summary("g", 5000, [1], "summary text")
    tmp_db.add_thread(summary_id, "user-aci", "question", 5001)
    tmp_db.add_thread(summary_id, None, "bot answer", 5002)

    assert tmp_db.is_bot_message_ts(5000, bot_aci=bot_aci) is True
    assert tmp_db.is_bot_message_ts(5001, bot_aci=bot_aci) is False
    assert tmp_db.is_bot_message_ts(5002, bot_aci=bot_aci) is True
    assert tmp_db.is_bot_message_ts(9999, bot_aci=bot_aci, quote_author_aci=bot_aci) is True
