import time
import uuid

from sigsummerrise.db import REDACTED_SUMMARY, Database


def test_opt_out_deletes_only_caller(tmp_db: Database):
    alice = str(uuid.uuid4())
    bob = str(uuid.uuid4())
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.upsert_user(bob, "Bob")
    now = int(time.time())
    tmp_db.opt_in(alice, now)
    tmp_db.opt_in(bob, now)
    tmp_db.insert_body(alice, 1, "alice secret")
    tmp_db.insert_body(bob, 2, "bob secret")
    tmp_db.insert_hole(3)
    sid = tmp_db.save_summary("g", 99, [1], "sum")
    tmp_db.add_thread(sid, alice, "question", 4)
    tmp_db.add_thread(sid, bob, "other", 5)
    tmp_db.create_magic_token(alice, "tok", now + 100)
    tmp_db.create_session(alice, "sess", now + 100)

    tmp_db.opt_out(alice)

    assert tmp_db.count_bodies(alice) == 0
    assert tmp_db.count_bodies(bob) == 1
    assert tmp_db.get_user(alice).consent_state == "declined"
    assert tmp_db.get_user(bob).opted_in
    holes = tmp_db.connect().execute("SELECT COUNT(*) AS n FROM messages WHERE is_hole = 1").fetchone()
    assert holes["n"] == 1
    thread = tmp_db.get_thread(sid)
    assert all(entry.sender_aci != alice for entry in thread)
    assert any(entry.sender_aci == bob for entry in thread)
    assert tmp_db.redeem_magic_token("tok", now) is None
    assert tmp_db.get_session_aci("sess", now) is None
    names = [row.display_name for row in tmp_db.dashboard_rows()]
    assert "Alice" not in names
    assert "Bob" in names
    assert alice not in str(names)
    summary = tmp_db.get_summary_by_id(sid)
    assert summary is not None
    assert summary.summary_text == REDACTED_SUMMARY
    assert "sum" not in summary.summary_text
