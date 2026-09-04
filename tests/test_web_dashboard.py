import time
import uuid

import pytest
from fastapi.testclient import TestClient

from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.main import create_app


def _client(tmp_path, settings):
    db = Database(str(tmp_path / "web.db"), settings.db_key)
    db.init()
    app = create_app(settings=settings, db=db, start_bot=False)
    return TestClient(app), db


def test_unauthenticated_hides_group_and_model(tmp_path, settings):
    settings = settings.model_copy(update={"group_name": "μ's", "openrouter_model": "secret/model"})
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, int(time.time()))
    db.insert_body(aci, 1, "secret body")
    response = client.get("/")
    assert response.status_code == 200
    assert "μ's" not in response.text
    assert "secret/model" not in response.text
    assert "Suisei" not in response.text
    assert "secret body" not in response.text


def test_authenticated_shows_usage_not_bodies(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1_700_000_000)
    db.insert_body(aci, 1, "secret body")
    now = int(time.time())
    from sigsummerrise import auth

    url = auth.issue_magic_link(db, settings, aci, now)
    token = url.rsplit("/", 1)[-1]
    client.get(f"/a/{token}", follow_redirects=False)
    dash = client.get("/")
    assert dash.status_code == 200
    assert "Suisei" in dash.text
    assert "secret body" not in dash.text
    assert "LLM calls" in dash.text or "LLM (24h)" in dash.text


def test_ops_disabled_without_token(tmp_path, settings):
    settings = settings.model_copy(update={"operator_token": ""})
    client, _db = _client(tmp_path, settings)
    assert client.get("/ops").status_code == 404


def test_ops_requires_auth(tmp_path, settings):
    settings = settings.model_copy(update={"operator_token": "sekrit-ops-token"})
    client, _db = _client(tmp_path, settings)
    assert client.get("/ops").status_code == 200
    assert "Operator token" in client.get("/ops").text
    bad = client.post("/ops/login", data={"token": "wrong"}, follow_redirects=False)
    assert bad.status_code == 401


def test_logout_clears_session(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1)
    from sigsummerrise import auth

    url = auth.issue_magic_link(db, settings, aci, int(time.time()))
    client.get(f"/a/{url.rsplit('/', 1)[-1]}", follow_redirects=False)
    assert "Suisei" in client.get("/").text
    raw_session = client.cookies.get(settings.session_cookie_name)
    client.post("/logout", follow_redirects=False)
    assert "Suisei" not in client.get("/").text
    assert db.get_session_aci(raw_session, int(time.time())) is None


def test_logout_clears_secure_host_cookie(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "secure.db"),
        db_key="unit-test-sqlcipher-key",
        public_base_url="https://example.com",
        signal_group_id="abc123",
        signal_bot_aci="11111111-1111-1111-1111-111111111111",
        responses_path="copy/responses.example.json",
    )
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1)
    from sigsummerrise import auth

    url = auth.issue_magic_link(db, settings, aci, int(time.time()))
    client.get(f"/a/{url.rsplit('/', 1)[-1]}", follow_redirects=False)
    logout = client.post("/logout", follow_redirects=False)
    set_cookie = logout.headers.get_list("set-cookie")
    assert any("__Host-ssr_session=" in header and "Secure" in header for header in set_cookie)
    assert "Suisei" not in client.get("/").text


def _login(client, db, settings, aci: str) -> None:
    from sigsummerrise import auth

    url = auth.issue_magic_link(db, settings, aci, int(time.time()))
    token = url.rsplit("/", 1)[-1]
    client.get(f"/a/{token}", follow_redirects=False)


def test_api_live_unauthenticated(tmp_path, settings):
    settings = settings.model_copy(update={"group_name": "μ's", "bot_name": "SecretBot"})
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, int(time.time()))
    response = client.get("/api/live")
    assert response.status_code == 401
    assert "μ's" not in response.text
    assert "SecretBot" not in response.text
    assert "Suisei" not in response.text


def test_api_live_authenticated(tmp_path, settings):
    settings = settings.model_copy(update={"bot_name": "TestBot"})
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, int(time.time()))
    _login(client, db, settings, aci)
    response = client.get("/api/live")
    assert response.status_code == 200
    data = response.json()
    assert data["bot_name"] == "TestBot"
    assert data["status"]["state"] == "idle"
    assert "awaiting messages" in data["status"]["message"]
    assert "opted_in" in data["stats"]
    assert data["quota"]["limit"] >= 1


def test_api_live_dm_privacy(tmp_path, settings):
    from sigsummerrise import activity

    settings = settings.model_copy(update={"bot_name": "TestBot"})
    client, db = _client(tmp_path, settings)
    alice = str(uuid.uuid4())
    bob = str(uuid.uuid4())
    db.upsert_user(alice, "Alice")
    db.upsert_user(bob, "Bob")
    db.opt_in(alice, int(time.time()))
    db.opt_in(bob, int(time.time()))
    activity.set_working(
        channel="dm",
        mode="ask",
        target_aci=alice,
        target_display_name="Alice",
        started_at=int(time.time()) - 10,
    )
    _login(client, db, settings, alice)
    alice_resp = client.get("/api/live").json()
    assert "reply to you" in alice_resp["status"]["message"]
    assert "Alice" not in alice_resp["status"]["message"]

    client.post("/logout", follow_redirects=False)
    _login(client, db, settings, bob)
    bob_resp = client.get("/api/live").json()
    assert "private reply" in bob_resp["status"]["message"]
    assert "Alice" not in bob_resp["status"]["message"]


def test_api_live_draft_only_for_target(tmp_path, settings):
    from sigsummerrise import activity

    settings = settings.model_copy(update={"bot_name": "TestBot"})
    client, db = _client(tmp_path, settings)
    alice = str(uuid.uuid4())
    bob = str(uuid.uuid4())
    db.upsert_user(alice, "Alice")
    db.upsert_user(bob, "Bob")
    db.opt_in(alice, int(time.time()))
    db.opt_in(bob, int(time.time()))
    activity.set_working(
        channel="group",
        mode="ask",
        target_aci=alice,
        target_display_name="Alice",
        started_at=int(time.time()),
    )
    activity.append_draft("streaming answer")

    _login(client, db, settings, alice)
    alice_resp = client.get("/api/live").json()
    assert alice_resp.get("draft") == "streaming answer"

    client.post("/logout", follow_redirects=False)
    _login(client, db, settings, bob)
    bob_resp = client.get("/api/live").json()
    assert "draft" not in bob_resp


def test_merged_members_table(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    alice = str(uuid.uuid4())
    bob = str(uuid.uuid4())
    db.upsert_user(alice, "Alice")
    db.upsert_user(bob, "Bob")
    db.opt_in(alice, int(time.time()))
    _login(client, db, settings, alice)
    dash = client.get("/")
    assert dash.status_code == 200
    assert "not opted-in" in dash.text
    assert 'badge bad">not opted-in' in dash.text or "badge bad\">not opted-in" in dash.text
    assert dash.text.index("opted in") < dash.text.index("not opted-in")
    assert "<h2>Not opted in</h2>" not in dash.text


def test_save_privacy_flags(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Alice")
    db.opt_in(aci, int(time.time()))
    _login(client, db, settings, aci)
    response = client.post(
        "/privacy",
        data={"exclude_from_summaries": "on", "exclude_from_questions": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    user = db.get_user(aci)
    assert user is not None
    assert user.exclude_from_summaries
    assert user.exclude_from_questions


def test_dashboard_opt_out_deletes_and_logs_out(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Alice")
    db.opt_in(aci, int(time.time()))
    db.insert_body(aci, 1, "stored")
    _login(client, db, settings, aci)
    response = client.post("/opt-out", follow_redirects=False)
    assert response.status_code == 302
    assert db.count_bodies(aci) == 0
    assert db.get_user(aci) is not None
    assert not db.get_user(aci).opted_in
    dash = client.get("/")
    assert "Alice" not in dash.text

