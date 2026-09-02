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


def test_require_runtime_settings():
    with pytest.raises(SystemExit, match="DB_KEY"):
        require_runtime_settings(Settings(db_key="", signal_group_id="g"))
    with pytest.raises(SystemExit, match="SIGNAL_GROUP_ID"):
        require_runtime_settings(Settings(db_key="k", signal_group_id=""))
    require_runtime_settings(Settings(db_key="k", signal_group_id="abc"))
