"""Sign-in routes: the session endpoint, same-origin enforcement, one-time
links, self sign-up, password change, renaming, safe redirects, and errors
that never echo a password."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from hubzoid.auth import links, oidc, routes, sessions, users
from hubzoid.auth.schema import engine_for

ENV = (
    "HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
    "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD",
    "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD", "HUBZOID_GATEWAY_ADMIN_EMAIL",
    "HUBZOID_DEPLOYMENT", "DATABASE_URL", "ENABLE_SIGNUP", "ENABLE_LOGIN_FORM",
    "ENABLE_PASSWORD_AUTH", "HUBZOID_LINK_HOURS", "WEBUI_NAME", "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET",
    "OPENID_PROVIDER_URL", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_PROVIDER_NAME",
)
ORIGIN = {"origin": "http://testserver"}
PASSWORD = "correct horse battery"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales Helper\n---\nHelp.\n")
    sessions.reset_cache()
    yield d
    sessions.reset_cache()


def client(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)
    return TestClient(app)


def person(hub, email="ana@example.com", password=PASSWORD, **kw):
    user = users.create(hub, email=email, name="Ana", password=password, **kw)
    users.sync_identity(hub, user)
    return user


def sign_in(c, email="ana@example.com", password=PASSWORD):
    return c.post("/api/auth/login", json={"email": email, "password": password}, headers=ORIGIN)


def me(c):
    return c.get("/api/auth/session").json().get("user")


# ---- session endpoint --------------------------------------------------------------

def test_session_lists_providers_and_options(hub, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OPENID_PROVIDER_URL", "https://idp.example.com")
    monkeypatch.setenv("OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_PROVIDER_NAME", "Acme SSO")
    monkeypatch.setenv("ENABLE_SIGNUP", "true")
    body = client(hub).get("/api/auth/session").json()
    assert body == {"authenticated": False, "mode": "accounts",
                    "providers": [{"id": "google", "name": "Google"},
                                  {"id": "oidc", "name": "Acme SSO"}],
                    "password": True, "signup": True, "branding_name": "Sales Helper"}
    monkeypatch.setenv("ENABLE_LOGIN_FORM", "false")
    assert client(hub).get("/api/auth/session").json()["password"] is False


def test_signed_in_session_describes_the_person(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    user = me(c)
    assert user["email"] == "ana@example.com" and user["role"] == "user"
    assert user["method"] == "password" and user["has_password"] and user["password_enabled"]
    assert "password_hash" not in str(c.get("/api/auth/session").json())


def test_password_sign_in_can_be_turned_off(hub, monkeypatch):
    person(hub)
    monkeypatch.setenv("ENABLE_PASSWORD_AUTH", "false")
    r = sign_in(client(hub))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "password_disabled"


def test_wrong_password_and_unknown_email_look_the_same(hub):
    person(hub)
    c = client(hub)
    a, b = sign_in(c, password="not the password"), sign_in(c, email="nobody@example.com")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": {"code": "invalid_credentials",
                                               "message": "The email or password is not right."}}


def test_errors_never_echo_the_password(hub):
    c = client(hub)
    secret = "my-secret-" + "x" * 5000
    for path, body in (("/api/auth/login", {"email": "a@example.com", "password": secret}),
                       ("/api/auth/signup", {"email": "a", "name": "A", "password": secret}),
                       ("/api/auth/login", {"email": "a@example.com", "password": 12345678})):
        r = c.post(path, json=body, headers=ORIGIN)
        assert r.status_code in (403, 422)
        assert "my-secret" not in r.text and "12345678" not in r.text


def test_rehashes_migrated_bcrypt_on_sign_in(hub):
    import bcrypt

    legacy = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    user = users.create(hub, email="old@example.com", password_hash=legacy, source="migrated")
    assert sign_in(client(hub), email="old@example.com").status_code == 200
    assert users.store(hub).password_hash(user["id"]).startswith("$argon2id$")


# ---- same origin ---------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {}, {"origin": "https://evil.example.com"}, {"origin": "null"},
    {"referer": "https://evil.example.com/page"},
])
def test_mutations_from_another_site_are_refused(hub, headers):
    person(hub)
    c = client(hub)
    for path, body in (("/api/auth/login", {"email": "ana@example.com", "password": PASSWORD}),
                       ("/api/auth/logout", None),
                       ("/api/auth/signup", {"email": "b@example.com", "name": "B", "password": PASSWORD}),
                       ("/api/auth/password", {"current_password": PASSWORD, "new_password": "x" * 12}),
                       ("/api/auth/link/abc", {"password": "x" * 12})):
        r = c.post(path, json=body, headers=headers)
        assert r.status_code == 403, path
        assert r.json()["detail"]["code"] == "cross_origin"
    r = c.patch("/api/auth/me", json={"name": "Eve"}, headers=headers)
    assert r.status_code == 403


def test_same_origin_by_referer_and_by_allowed_origin(hub, monkeypatch):
    person(hub)
    c = client(hub)
    r = c.post("/api/auth/login", json={"email": "ana@example.com", "password": PASSWORD},
               headers={"referer": "http://testserver/auth?redirect=/"})
    assert r.status_code == 200
    # A second public name of the deployment, although the request's Host differs.
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://chat.example.org")
    for origin in ("https://hub.example.com", "https://chat.example.org"):
        r = c.post("/api/auth/login", json={"email": "ana@example.com", "password": PASSWORD},
                   headers={"origin": origin})
        assert r.status_code == 200, origin
    r = c.post("/api/auth/login", json={"email": "ana@example.com", "password": PASSWORD},
               headers={"origin": "https://hub.example.com.evil.net"})
    assert r.status_code == 403


# ---- one-time links -------------------------------------------------------------------

def test_link_sets_the_password_signs_in_and_works_once(hub):
    user = person(hub, password=None)
    other = client(hub)
    token, expires = links.create(hub, user["id"], created_by="admin@example.com")
    assert expires - time.time() > 71 * 3600
    c = client(hub)
    info = c.get(f"/api/auth/link/{token}")
    assert info.json() == {"valid": True, "purpose": "set_password", "email": "ana@example.com"}
    assert info.headers["referrer-policy"] == "no-referrer"
    short = c.post(f"/api/auth/link/{token}", json={"password": "short"}, headers=ORIGIN)
    assert short.status_code == 422 and short.json()["detail"]["code"] == "invalid_password"
    assert c.get(f"/api/auth/link/{token}").json()["valid"]  # a refused password keeps the link
    r = c.post(f"/api/auth/link/{token}", json={"password": "my new password"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"]["email"] == "ana@example.com"
    assert r.json()["user"]["method"] == "link"
    assert me(c)["email"] == "ana@example.com"
    again = other.post(f"/api/auth/link/{token}", json={"password": "another password"},
                       headers=ORIGIN)
    assert again.status_code == 410 and again.json()["detail"]["code"] == "link_invalid"
    assert other.get(f"/api/auth/link/{token}").json() == {"valid": False, "purpose": None,
                                                            "email": None}
    assert sign_in(other, password="my new password").status_code == 200


def test_using_a_link_ends_other_sessions(hub):
    user = person(hub)
    phone = client(hub)
    sign_in(phone)
    token, _ = links.create(hub, user["id"], purpose="reset_password")
    c = client(hub)
    assert c.post(f"/api/auth/link/{token}", json={"password": "fresh password"},
                  headers=ORIGIN).status_code == 200
    assert me(phone) is None and me(c)["email"] == "ana@example.com"


def test_expired_replaced_and_unusable_links(hub, monkeypatch):
    from hubzoid.access import store_for

    user = person(hub, password=None)
    c = client(hub)
    old, _ = links.create(hub, user["id"])
    new, _ = links.create(hub, user["id"])
    assert not c.get(f"/api/auth/link/{old}").json()["valid"]  # replaced by the newer link
    with engine_for(hub).begin() as conn:
        conn.execute(text("UPDATE hz_auth_links SET expires_at=:t"), {"t": time.time() - 1})
    assert not c.get(f"/api/auth/link/{new}").json()["valid"]
    assert c.post(f"/api/auth/link/{new}", json={"password": "long enough"},
                  headers=ORIGIN).status_code == 410
    fresh, _ = links.create(hub, user["id"])
    store_for(hub).suspend("ana@example.com", actor="test")
    assert not c.get(f"/api/auth/link/{fresh}").json()["valid"]
    waiting = person(hub, email="w@example.com", password=None, status="pending")
    token, _ = links.create(hub, waiting["id"])
    assert not c.get(f"/api/auth/link/{token}").json()["valid"]
    assert c.get("/api/auth/link/not-a-token").json()["valid"] is False
    monkeypatch.setenv("HUBZOID_LINK_HOURS", "1")
    _, expires = links.create(hub, user["id"])
    assert expires - time.time() <= 3600


def test_a_password_change_cancels_outstanding_links(hub):
    user = person(hub)
    token, _ = links.create(hub, user["id"], purpose="reset_password")
    c = client(hub)
    sign_in(c)
    c.post("/api/auth/password", headers=ORIGIN,
           json={"current_password": PASSWORD, "new_password": "changed by me"})
    assert not c.get(f"/api/auth/link/{token}").json()["valid"]


# ---- sign-up ----------------------------------------------------------------------------

def test_signup_is_off_by_default(hub):
    r = client(hub).post("/api/auth/signup", headers=ORIGIN,
                         json={"email": "new@example.com", "name": "New", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "signup_disabled"
    assert users.find_by_email(hub, "new@example.com") is None


def test_signup_creates_a_pending_account_until_approved(hub, monkeypatch):
    from hubzoid.access import store_for

    monkeypatch.setenv("ENABLE_SIGNUP", "true")
    c = client(hub)
    r = c.post("/api/auth/signup", headers=ORIGIN,
               json={"email": "New@Example.com", "name": "New", "password": PASSWORD})
    assert r.status_code == 201 and r.json() == {"status": "pending"}
    user = users.find_by_email(hub, "new@example.com")
    assert (user["status"], user["role"], user["source"]) == ("pending", "user", "signup")
    identity = store_for(hub).identity("new@example.com")
    assert identity["owui_id"] == user["id"] and identity["pending"]  # People shows it
    # The status is only told with the right password.
    assert sign_in(c, email="new@example.com", password="wrong password").status_code == 401
    pending = sign_in(c, email="new@example.com")
    assert pending.status_code == 403 and pending.json()["detail"]["code"] == "pending"
    users.set_status(hub, user["id"], "active")
    assert sign_in(c, email="new@example.com").status_code == 200


def test_signup_refusals(hub, monkeypatch):
    monkeypatch.setenv("ENABLE_SIGNUP", "true")
    person(hub)
    c = client(hub)
    cases = [({"email": "ana@example.com", "name": "Ana", "password": PASSWORD}, 409, "account_exists"),
             ({"email": "not-an-email", "name": "X", "password": PASSWORD}, 422, "invalid_email"),
             ({"email": "x@example.com", "name": "  ", "password": PASSWORD}, 422, "invalid_name"),
             ({"email": "x@example.com", "name": "X", "password": "short"}, 422, "invalid_password"),
             ({"email": "x@example.com", "name": "X"}, 422, "invalid_request")]
    for body, status, code in cases:
        r = c.post("/api/auth/signup", json=body, headers=ORIGIN)
        assert (r.status_code, r.json()["detail"]["code"]) == (status, code), body


# ---- the signed-in person ---------------------------------------------------------------

def test_password_change_rules(hub):
    person(hub)
    c = client(hub)
    assert c.post("/api/auth/password", headers=ORIGIN,
                  json={"current_password": PASSWORD, "new_password": "new secret!"}).status_code == 401
    sign_in(c)
    wrong = c.post("/api/auth/password", headers=ORIGIN,
                   json={"current_password": "not it at all", "new_password": "new secret!"})
    assert wrong.status_code == 401 and wrong.json()["detail"]["code"] == "invalid_credentials"
    weak = c.post("/api/auth/password", headers=ORIGIN,
                  json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 422
    google = users.create(hub, email="g@example.com", password_enabled=False)
    token = sessions.create_session(hub, google, method="google")
    g = client(hub)
    g.cookies.set("hz_session", token)
    none = g.post("/api/auth/password", headers=ORIGIN,
                  json={"current_password": "anything at all", "new_password": "new secret!"})
    assert none.status_code == 409 and none.json()["detail"]["code"] == "no_password"


def test_rename_me(hub):
    person(hub)
    c = client(hub)
    assert c.patch("/api/auth/me", json={"name": "Ana B"}, headers=ORIGIN).status_code == 401
    sign_in(c)
    r = c.patch("/api/auth/me", json={"name": "  Ana B  "}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"]["name"] == "Ana B"
    assert users.find_by_email(hub, "ana@example.com")["name"] == "Ana B"
    assert c.patch("/api/auth/me", json={"name": " "}, headers=ORIGIN).status_code == 422
    assert c.patch("/api/auth/me", json={"name": "x" * 201}, headers=ORIGIN).status_code == 422


def test_rename_the_local_owner(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_AUTH")
    c = client(hub)
    r = c.patch("/api/auth/me", json={"name": "Priya"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"]["email"] == "admin@localhost"
    assert me(c)["name"] == "Priya"


def test_sign_in_records_activity_without_secrets(hub):
    from hubzoid.access import store_for

    person(hub)
    c = client(hub)
    sign_in(c, password="wrong password here")
    sign_in(c)
    c.post("/api/auth/logout", headers=ORIGIN)
    rows = store_for(hub).read_access_audit(10, hubs=["*"])
    assert [r["action"] for r in rows] == ["signed_out", "signed_in", "sign_in_failed"]
    assert all(r["subject"] == "ana@example.com" and r["surface"] == "web" for r in rows)
    assert PASSWORD not in str(rows) and "wrong password" not in str(rows)


# ---- redirects --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("/c/abc", "/c/abc"), ("/c/abc?x=1#y", "/c/abc?x=1#y"), (None, "/"), ("", "/"),
    ("//evil.example.com", "/"), ("https://evil.example.com/x", "/"), ("/\\evil.example.com", "/"),
    ("javascript:alert(1)", "/"), ("c/abc", "/"), ("/ok\r\nSet-Cookie: x=1", "/"),
    ("/%2F%2Fevil.example.com", "/%2F%2Fevil.example.com"),
    ("/\t/evil.example.com", "/"), ("/\n/evil.example.com", "/"), ("\t//evil.example.com", "/"),
    ("/\t\\evil.example.com", "/"), ("/c/a\x00b", "/"), ("/c/a\x7fb", "/"),
])
def test_safe_redirect(value, expected):
    assert oidc.safe_redirect(value) == expected


def test_mount_creates_the_first_administrator(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", "owner password 1")
    c = client(hub)
    r = sign_in(c, email="owner@example.com", password="owner password 1")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"


def test_the_bridge_serves_sign_in_with_the_web_app(tmp_path, monkeypatch):
    """server.build_app mounts the sign-in routes in the default mode: the
    first administrator signs in and reaches agents and the Console."""
    import shutil
    from pathlib import Path

    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    hub = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal_hub", hub)
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", "owner password 1")
    sessions.reset_cache()
    from hubzoid.server import build_app

    c = TestClient(build_app())
    assert c.get("/api/auth/session").json()["authenticated"] is False
    assert c.get("/api/agents").status_code == 401
    assert sign_in(c, email="owner@example.com", password="owner password 1").status_code == 200
    assert c.get("/api/auth/session").json()["user"]["role"] == "admin"
    assert c.get("/api/agents").status_code == 200
    me = c.get("/portal/api/me").json()
    assert me["org_admin"] is True and me["sign_in"]["links"] is True


def test_access_logs_never_hold_link_tokens_or_oauth_codes(hub):
    """uvicorn logs the path with its query string; mounting the sign-in routes
    installs a filter that redacts one-time credentials first."""
    import logging

    from hubzoid.auth import logredact

    client(hub)  # mounting installs the filter
    logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, logredact.RedactCredentials) for f in logger.filters)
    record = logger.makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.7:5000", "GET", "/auth/set-password?token=SECRET-TOKEN&x=1", "1.1", 200), None)
    for f in logger.filters:
        f.filter(record)
    assert "SECRET-TOKEN" not in record.getMessage() and "token=[redacted]&x=1" in record.getMessage()
    assert logredact.redact("/api/auth/link/SECRET-TOKEN") == "/api/auth/link/[redacted]"
    assert logredact.redact("/oauth/google/callback?code=C0DE&state=S7") == \
        "/oauth/google/callback?code=[redacted]&state=[redacted]"
    assert logredact.redact("/c/abc?q=1") == "/c/abc?q=1"


def test_the_local_owner_never_signs_in_with_a_password(hub):
    """Open WebUI created admin@localhost with the password "admin"; with
    sign-in on, that account must not open with it (nor with a link)."""
    import bcrypt

    owner = users.create(hub, email="admin@localhost", role="admin", source="migrated",
                         password_hash=bcrypt.hashpw(b"admin", bcrypt.gensalt(4)).decode())
    c = client(hub)
    r = sign_in(c, email="admin@localhost", password="admin")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    with pytest.raises(ValueError):
        links.create(hub, owner["id"])
    assert users.ensure_local_owner(hub)["id"] == owner["id"]  # the same account, never a second


def test_a_rehash_moves_updated_at_and_the_sign_in_goes_ahead(hub):
    import bcrypt

    legacy = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    user = users.create(hub, email="old2@example.com", password_hash=legacy, source="migrated")
    assert sign_in(client(hub), email="old2@example.com").status_code == 200
    assert users.get(hub, user["id"])["updated_at"] > user["updated_at"]


def test_current_user_is_thread_safe(hub, monkeypatch):
    """Chat runs resolve the person from worker threads."""
    import threading

    from fastapi import Request as StarletteRequest

    person(hub)
    token = sessions.create_session(hub, users.find_by_email(hub, "ana@example.com"),
                                    method="password")
    from hubzoid.auth import current_user

    def make(cookie):
        headers = [(b"host", b"localhost")]
        if cookie:
            headers.append((b"cookie", f"hz_session={cookie}".encode()))
        return StarletteRequest({"type": "http", "method": "GET", "path": "/", "headers": headers,
                                 "query_string": b"", "server": ("127.0.0.1", 3080)})

    seen, errors = [], []

    def work(mode):
        try:
            for _ in range(5):
                user = current_user(make(token if mode == "accounts" else None), hub)
                seen.append((mode, user.email if user else None))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=("accounts",)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.delenv("HUBZOID_AUTH")
    sessions.reset_cache()
    threads = [threading.Thread(target=work, args=("local",)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert {e for m, e in seen if m == "accounts"} == {"ana@example.com"}
    assert {e for m, e in seen if m == "local"} == {"admin@localhost"}
    assert len(users.list_users(hub)) == 2  # one local owner, however many threads raced
