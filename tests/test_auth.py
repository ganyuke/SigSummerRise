import time
import uuid

from fastapi.testclient import TestClient

from sigsummerrise import auth
from sigsummerrise.db import Database
from sigsummerrise.main import create_app


def _client(tmp_path, settings):
    db = Database(str(tmp_path / "web.db"), settings.db_key)
    db.init()
    app = create_app(settings=settings, db=db, start_bot=False)
    return TestClient(app), db


def test_unauthenticated_has_no_data(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, int(time.time()))
    db.insert_body(aci, 1, "secret body")
    response = client.get("/")
    assert response.status_code == 200
    assert "secret body" not in response.text
    assert aci not in response.text
    assert "Suisei" not in response.text
    assert "Mention the bot" in response.text or "mention the bot" in response.text.lower()
    assert "dashboard" in response.text.lower()


def test_magic_link_single_use_and_cookie(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1_700_000_000)
    db.insert_body(aci, 1, "secret body")
    now = int(time.time())
    url = auth.issue_magic_link(db, settings, aci, now)
    assert url is not None
    token = url.rsplit("/", 1)[-1]
    first = client.get(f"/a/{token}", follow_redirects=False)
    assert first.status_code == 302
    assert first.headers["location"] == "/"
    assert "no-store" in first.headers.get("cache-control", "").lower()
    cookie = first.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "ssr_session" in cookie
    assert token not in cookie
    second = client.get(f"/a/{token}", follow_redirects=False)
    assert second.status_code == 400
    dash = client.get("/")
    assert dash.status_code == 200
    assert "Suisei" in dash.text
    assert "1" in dash.text
    assert aci not in dash.text
    assert "secret body" not in dash.text


def test_expired_token_rejected(tmp_path, settings):
    client, db = _client(tmp_path, settings)
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1)
    db.create_magic_token(aci, "expiredtokenvalue", expires_at=1)
    response = client.get("/a/expiredtokenvalue")
    assert response.status_code == 400
    assert "Suisei" not in response.text


def test_rate_limit(tmp_path, settings):
    db = Database(str(tmp_path / "rl.db"), settings.db_key)
    db.init()
    aci = str(uuid.uuid4())
    db.upsert_user(aci, "Suisei")
    db.opt_in(aci, 1)
    now = 1_800_000_000
    for _ in range(3):
        assert auth.issue_magic_link(db, settings, aci, now) is not None
    assert auth.issue_magic_link(db, settings, aci, now) is None


def test_openapi_disabled(tmp_path, settings):
    client, _db = _client(tmp_path, settings)
    assert client.get("/openapi.json").status_code == 404
