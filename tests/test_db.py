import pytest

from sigsummerrise.config import Settings
from sigsummerrise.db import REDACTED_SUMMARY, Database, Mention, merge_message_windows
from sigsummerrise.main import require_runtime_settings


def test_insert_body_round_trips_mentions(tmp_db: Database):
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    mention = Mention(uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", start=0)
    tmp_db.insert_body(aci, 10, "\ufffc hello", mentions=[mention])
    stored = tmp_db.last_n_kept(1)[0]
    assert stored.mentions == (mention,)


def test_insert_body_without_mentions_defaults_empty(tmp_db: Database):
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.insert_body(aci, 10, "hello")
    stored = tmp_db.last_n_kept(1)[0]
    assert stored.mentions == ()


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


def test_delete_message_at(tmp_db: Database):
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Alice")
    msg_id = tmp_db.insert_body(aci, 100, "bye")
    sid = tmp_db.save_summary("g", 5000, [msg_id], "sum")
    assert tmp_db.delete_message_at(aci, 100) is True
    assert tmp_db.count_bodies(aci) == 0
    assert tmp_db.get_summary_by_id(sid).summary_text == REDACTED_SUMMARY
    assert tmp_db.delete_message_at(aci, 100) is False


def test_is_bot_message_ts(tmp_db: Database):
    bot_aci = "11111111-1111-1111-1111-111111111111"
    summary_id = tmp_db.save_summary("g", 5000, [1], "summary text")
    tmp_db.add_thread(summary_id, "user-aci", "question", 5001)
    tmp_db.add_thread(summary_id, None, "bot answer", 5002)

    assert tmp_db.is_bot_message_ts(5000, bot_aci=bot_aci) is True
    assert tmp_db.is_bot_message_ts(5001, bot_aci=bot_aci) is False
    assert tmp_db.is_bot_message_ts(5002, bot_aci=bot_aci) is True
    assert tmp_db.is_bot_message_ts(9999, bot_aci=bot_aci, quote_author_aci=bot_aci) is True


def test_get_message_at_and_messages_around(tmp_db: Database):
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bob = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.upsert_user(bob, "Bob")
    for ts, aci, body in (
        (10, alice, "one"),
        (20, bob, "two"),
        (30, alice, "anchor"),
        (40, bob, "four"),
        (50, alice, "five"),
    ):
        tmp_db.insert_body(aci, ts, body)
    anchor = tmp_db.get_message_at(alice, 30)
    assert anchor is not None
    assert anchor.body == "anchor"
    around = tmp_db.messages_around(30, before=1, after=1)
    assert [message.body for message in around] == ["two", "anchor", "four"]


def test_merge_message_windows_prefers_anchor(tmp_db: Database):
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(alice, "Alice")
    anchor = []
    recent = []
    for ts in range(1, 6):
        msg_id = tmp_db.insert_body(alice, ts, f"m{ts}")
        message = tmp_db.get_message_at(alice, ts)
        assert message is not None
        if ts <= 2:
            anchor.append(message)
        recent.append(message)
    merged = merge_message_windows(anchor, recent, max_n=4)
    bodies = [message.body for message in merged]
    assert "m1" in bodies
    assert "m2" in bodies
    assert len(merged) == 4


def test_opt_out_anonymizes_bodies_to_holes(tmp_db: Database):
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.opt_in(alice, 1)
    msg_id = tmp_db.insert_body(alice, 100, "secret")
    tmp_db.opt_out(alice)
    row = tmp_db.connect().execute(
        "SELECT sender_aci, body, is_hole FROM messages WHERE id = ?",
        (msg_id,),
    ).fetchone()
    assert row is not None
    assert row["sender_aci"] is None
    assert row["body"] is None
    assert row["is_hole"] == 1
    assert tmp_db.count_bodies(alice) == 0


def test_init_migrates_pre_mentions_schema(tmp_path):
    """An old DB without mentions_json must migrate on init and keep working."""
    import sqlcipher3

    path = str(tmp_path / "legacy.db")
    key = "unit-test-sqlcipher-key"
    conn = sqlcipher3.dbapi2.connect(path)
    conn.execute(f"PRAGMA key = '{key}'")
    conn.execute("PRAGMA cipher_compatibility = 4")
    conn.executescript(
        """
        CREATE TABLE users (
            aci TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            consent_state TEXT NOT NULL DEFAULT 'unknown',
            opted_in_at INTEGER,
            last_consent_dm_at INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_aci TEXT,
            ts INTEGER NOT NULL,
            body TEXT,
            is_hole INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO users (aci, display_name, consent_state) VALUES (?, ?, 'opted_in')",
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "Alice"),
    )
    conn.execute(
        "INSERT INTO messages (sender_aci, ts, body, is_hole) VALUES (?, 10, 'old body', 0)",
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
    )
    conn.commit()
    conn.close()

    db = Database(path, key)
    db.init()
    try:
        stored = db.last_n_kept(10)[0]
        assert stored.body == "old body"
        assert stored.mentions == ()
        alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        mention = Mention(uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", start=0)
        db.insert_body(alice, 11, "\ufffc new", mentions=[mention])
        stored = db.last_n_kept(1)[0]
        assert stored.mentions == (mention,)
    finally:
        db.close()
