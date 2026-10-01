"""Sessions: the cookie, expiry (absolute and idle), last-seen throttling,
revocation on password, role, status and deletion, suspension, local mode."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from hubzoid.auth import current_user, routes, sessions, users
from hubzoid.auth.schema import engine_for

ENV = (
    "HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
    "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD",
    "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD", "HUBZOID_GATEWAY_ADMIN_EMAIL",
    "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_SESSION_DAYS", "HUBZOID_SESSION_IDLE_DAYS",
    "ENABLE_SIGNUP", "ENABLE_LOGIN_FORM", "ENABLE_PASSWORD_AUTH", "HUBZOID_AUTH_MAX_FAILURES",
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
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    sessions.reset_cache()
    yield d
    sessions.reset_cache()


def client(hub, **kw) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)

    @app.get("/whoami")
    def whoami(request: Request):
        user = current_user(request, hub)
        return {"email": user.email if user else None}

    return TestClient(app, **kw)


def person(hub, email="ana@example.com", **kw):
    user = users.create(hub, email=email, name="Ana", password=PASSWORD, **kw)
    users.sync_identity(hub, user)
    return user


def sign_in(c, email="ana@example.com", password=PASSWORD, **headers):
    return c.post("/api/auth/login", json={"email": email, "password": password},
                  headers={**ORIGIN, **headers})


def who(c) -> str | None:
    return c.get("/whoami").json()["email"]


def rows(hub):
    with engine_for(hub).connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text("SELECT * FROM hz_sessions"))]


def set_row(hub, **values):
    sets = ", ".join(f"{k}=:{k}" for k in values)
    with engine_for(hub).begin() as conn:
        conn.execute(text(f"UPDATE hz_sessions SET {sets}"), values)


def test_cookie_attributes_and_stored_digest(hub):
    person(hub)
    c = client(hub)
    r = sign_in(c)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("hz_session=")
    for part in ("HttpOnly", "Path=/", "SameSite=lax", f"Max-Age={30 * 86400}"):
        assert part in cookie
    assert "Secure" not in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    assert len(token) >= 43
    stored = rows(hub)
    assert len(stored) == 1 and stored[0]["token_hash"] == sessions.digest(token)
    assert token not in str(stored)
    assert stored[0]["method"] == "password" and stored[0]["idle_seconds"] == 7 * 86400
    assert who(c) == "ana@example.com"


def test_secure_cookie_behind_https(hub):
    person(hub)
    r = sign_in(client(hub), **{"x-forwarded-proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_absolute_and_idle_expiry(hub, monkeypatch):
    person(hub)
    c = client(hub)
    sign_in(c)
    now = time.time()
    set_row(hub, expires_at=now - 1)
    assert who(c) is None
    set_row(hub, expires_at=now + 3600, created_at=now - 31 * 86400)  # older than 30 days
    assert who(c) is None
    set_row(hub, created_at=now, last_seen_at=now - 8 * 86400)  # idle 8 days
    assert who(c) is None
    set_row(hub, last_seen_at=now - 2 * 86400)
    assert who(c) == "ana@example.com"
    monkeypatch.setenv("HUBZOID_SESSION_IDLE_DAYS", "1")  # lowering applies to existing sessions
    set_row(hub, last_seen_at=now - 2 * 86400)
    assert who(c) is None
    monkeypatch.setenv("HUBZOID_SESSION_DAYS", "0.5")
    set_row(hub, last_seen_at=now, created_at=now - 86400)
    assert who(c) is None


def test_last_seen_is_written_at_most_every_five_minutes(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    now = time.time()
    set_row(hub, last_seen_at=now - 100)
    assert who(c) == "ana@example.com"
    assert abs(rows(hub)[0]["last_seen_at"] - (now - 100)) < 1
    set_row(hub, last_seen_at=now - 400)
    assert who(c) == "ana@example.com"
    assert rows(hub)[0]["last_seen_at"] >= now - 5


def test_role_status_deletion_and_suspension_end_sessions(hub):
    from hubzoid.access import store_for

    user = person(hub)
    c = client(hub)
    sign_in(c)
    users.set_role(hub, user["id"], "admin")
    assert who(c) is None
    sign_in(c)
    users.set_status(hub, user["id"], "pending")
    assert who(c) is None
    users.set_status(hub, user["id"], "active")
    sign_in(c)
    store_for(hub).suspend("ana@example.com", actor="test")
    assert who(c) is None
    assert sign_in(c).json()["detail"]["code"] == "suspended"
    store_for(hub).suspend("ana@example.com", actor="test", suspended=False)
    assert sign_in(c).status_code == 200
    users.delete(hub, user["id"])
    assert who(c) is None


def test_password_change_ends_other_sessions_only(hub):
    person(hub)
    laptop, phone = client(hub), client(hub)
    sign_in(laptop)
    sign_in(phone)
    r = laptop.post("/api/auth/password", headers=ORIGIN,
                    json={"current_password": PASSWORD, "new_password": "a brand new secret"})
    assert r.status_code == 204
    assert who(laptop) == "ana@example.com"
    assert who(phone) is None
    assert sign_in(phone).status_code == 401
    assert sign_in(phone, password="a brand new secret").status_code == 200


def test_logout_revokes_this_session_and_clears_the_cookie(hub):
    person(hub)
    laptop, phone = client(hub), client(hub)
    sign_in(laptop)
    sign_in(phone)
    r = laptop.post("/api/auth/logout", headers=ORIGIN)
    assert r.status_code == 204
    assert 'hz_session=""' in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert who(laptop) is None
    assert who(phone) == "ana@example.com"
    assert sum(1 for s in rows(hub) if s["revoked_at"]) == 1


def test_signing_in_again_replaces_the_previous_session(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    first = c.cookies.get("hz_session")
    sign_in(c)
    assert c.cookies.get("hz_session") != first
    assert sessions.resolve_token(hub, first) is None


def test_revoke_helpers(hub):
    user = person(hub)
    a = sessions.create_session(hub, user, method="password")
    b = sessions.create_session(hub, user, method="password")
    assert sessions.resolve_token(hub, a).email == "ana@example.com"
    assert sessions.revoke_user(hub, user["id"], except_token=a) == 1
    assert sessions.resolve_token(hub, a) is not None and sessions.resolve_token(hub, b) is None
    assert sessions.revoke(hub, a) is True and sessions.revoke(hub, a) is False
    assert sessions.resolve_token(hub, "x" * 300) is None
    assert sessions.resolve_token(hub, "") is None


def test_a_change_racing_sign_in_leaves_no_session(hub):
    """The account changed between reading the credential and starting the
    session (a reset, a role change): nothing is kept."""
    user = person(hub)
    users.set_password(hub, user["id"], "changed meanwhile")  # bumps updated_at
    with pytest.raises(sessions.SessionRace):
        sessions.create_session(hub, user, method="password", expect_updated_at=user["updated_at"])
    assert rows(hub) == []
    fresh = users.get(hub, user["id"])
    token = sessions.create_session(hub, fresh, method="password",
                                    expect_updated_at=fresh["updated_at"])
    assert sessions.resolve_token(hub, token).email == "ana@example.com"
    users.set_status(hub, user["id"], "pending")
    stale = users.get(hub, user["id"])
    with pytest.raises(sessions.SessionRace):  # not active
        sessions.create_session(hub, stale, method="password", expect_updated_at=stale["updated_at"])


def test_sign_in_racing_a_reset_is_refused(hub, monkeypatch):
    """A password checked just before an administrator's reset commits can't
    start a session that outlives the reset."""
    person(hub)
    c = client(hub)
    real = users.UserStore.credentials

    def read_then_reset(self, email):
        found = real(self, email)
        self.set_password(found[0]["id"], "reset by the admin")  # lands mid sign-in
        return found

    monkeypatch.setattr(users.UserStore, "credentials", read_then_reset)
    r = sign_in(c)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sign_in_changed"
    assert who(c) is None and rows(hub) == []


def test_blocking_ends_sessions_for_good(hub):
    """Lifting a block never brings an old session (or a stolen cookie) back."""
    from hubzoid.access import store_for
    from hubzoid.access.service import AccessService, Actor

    gs = store_for(hub)
    gs.bootstrap(["boss@example.com"])
    person(hub)
    c = client(hub)
    sign_in(c)
    stolen = c.cookies.get("hz_session")
    service = AccessService(hub)
    boss = Actor("boss@example.com", "console", "session")
    service.set_blocked(boss, "ana@example.com", True)
    assert who(c) is None
    service.set_blocked(boss, "ana@example.com", False)
    assert sessions.resolve_token(hub, stolen) is None
    assert sign_in(c).status_code == 200  # signing in again works


def test_a_session_seen_while_blocked_is_revoked(hub):
    """Any other way of blocking: the session is ended the first time it is
    presented while blocked."""
    from hubzoid.access import store_for

    user = person(hub)
    token = sessions.create_session(hub, user, method="password")
    store_for(hub).suspend("ana@example.com", actor="test")
    assert sessions.resolve_token(hub, token) is None
    store_for(hub).suspend("ana@example.com", actor="test", suspended=False)
    assert sessions.resolve_token(hub, token) is None


def test_short_idle_limits_keep_active_sessions(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_SESSION_IDLE_DAYS", str(120 / 86400))  # two minutes
    person(hub)
    c = client(hub)
    sign_in(c)
    now = time.time()
    set_row(hub, last_seen_at=now - 90, idle_seconds=120)
    assert who(c) == "ana@example.com"
    assert rows(hub)[0]["last_seen_at"] >= now - 5  # touched although under five minutes


def test_store_failures_are_503_not_500(hub, monkeypatch):
    import hubzoid.access as access

    person(hub)
    c = client(hub)
    sign_in(c)

    def broken(_hub):
        raise RuntimeError("access store unavailable")

    monkeypatch.setattr(access, "store_for", broken)
    r = c.get("/whoami")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "accounts_unavailable"


def test_database_errors_fail_closed(hub, monkeypatch):
    from sqlalchemy.exc import OperationalError

    person(hub)
    c = client(hub)
    sign_in(c)

    def broken(_hub):
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(sessions, "engine_for", broken)
    r = c.get("/whoami")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "accounts_unavailable"


# ---- local mode -----------------------------------------------------------------

def test_local_mode_is_the_local_owner(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_AUTH")
    c = client(hub)
    body = c.get("/api/auth/session").json()
    assert body["authenticated"] and body["mode"] == "local"
    assert body["user"]["email"] == "admin@localhost" and body["user"]["role"] == "admin"
    assert body["providers"] == [] and body["password"] is False and body["signup"] is False
    assert who(c) == "admin@localhost"
    assert sign_in(c).json()["detail"]["code"] == "sign_in_off"
    owner = users.find_by_email(hub, "admin@localhost")
    assert owner["source"] == "local"
    assert sessions.local_owner(hub).id == owner["id"]


@pytest.mark.parametrize("host,allowed", [
    ("localhost:3080", True), ("127.0.0.1:3080", True), ("[::1]:3080", True),
    ("192.168.1.20:3080", True), ("app.localhost", True), ("testserver", True),
    ("evil.example.com", False), ("evil.example.com:3080", False),
    ("my-laptop:3080", False), ("evil", False),
])
def test_local_mode_refuses_other_host_names(hub, monkeypatch, host, allowed):
    """Sign-in off makes every request the owner; a web page reaching the
    server through DNS rebinding names it by the attacker's host name (a
    single-label name too, which a hostile network's resolver can answer).
    The name the server listens on (here the test client's) is fine."""
    monkeypatch.delenv("HUBZOID_AUTH")
    c = client(hub)
    seen = c.get("/whoami", headers={"host": host}).json()["email"]
    assert seen == ("admin@localhost" if allowed else None)


def test_a_forwarded_address_cannot_unlock_local_mode(hub, monkeypatch):
    """Only the Host and the listening socket decide: request headers such as
    X-Forwarded-For (which a page can set) change nothing."""
    monkeypatch.delenv("HUBZOID_AUTH")
    c = client(hub)
    r = c.get("/whoami", headers={"host": "evil.example.com", "x-forwarded-for": "testclient",
                                  "x-forwarded-host": "localhost"})
    assert r.json()["email"] is None


def test_local_mode_accepts_configured_names(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_AUTH")
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://hub.example.com")
    c = client(hub)
    assert c.get("/whoami", headers={"host": "hub.example.com"}).json()["email"] == "admin@localhost"


# ---- changes racing an administrator's action (review findings) ------------------------

def _live_sessions(hub) -> list[dict]:
    return [s for s in rows(hub) if s["revoked_at"] is None]


def _admin_reset(hub, user_id) -> str:
    """An administrator's "Reset password" in the Console: the password stops
    working, sessions end, and a one-time link is returned. Returns its token."""
    from hubzoid.access.accounts import HubzoidAccounts

    link = HubzoidAccounts(hub).reset_with_link(user_id, created_by="boss@example.com")["link"]
    return link.split("token=", 1)[1]


def test_a_password_change_racing_a_reset_does_not_undo_it(hub, monkeypatch):
    """Someone holding the current password and a session starts a password
    change; an administrator's reset lands while the current password is being
    checked. The change must not overwrite the reset, keep its session or
    cancel the administrator's link."""
    from hubzoid.auth import links, passwords

    user = person(hub)
    c = client(hub)
    sign_in(c)
    real = passwords.verify_and_update
    issued: list[str] = []

    def verify_then_reset(password, stored):
        result = real(password, stored)
        if not issued:
            issued.append(_admin_reset(hub, user["id"]))  # lands mid-request
        return result

    monkeypatch.setattr(passwords, "verify_and_update", verify_then_reset)
    r = c.post("/api/auth/password", headers=ORIGIN,
               json={"current_password": PASSWORD, "new_password": "the attacker's choice"})
    monkeypatch.setattr(passwords, "verify_and_update", real)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "account_changed"
    assert users.store(hub).password_hash(user["id"]) is None  # the reset stands
    assert who(c) is None and _live_sessions(hub) == []
    other = client(hub)
    assert sign_in(other, password="the attacker's choice").status_code == 401
    assert sign_in(other).status_code == 401
    assert links.inspect(hub, issued[0])["valid"]  # the administrator's link still works


def test_a_password_change_from_an_ended_session_is_refused(hub, monkeypatch):
    """The session making the change is checked again when the change is
    written: one ended meanwhile (signed out everywhere) changes nothing."""
    from hubzoid.auth import passwords

    user = person(hub)
    c = client(hub)
    sign_in(c)
    real = passwords.verify_and_update

    def verify_then_end_sessions(password, stored):
        result = real(password, stored)
        sessions.revoke_user(hub, user["id"])
        return result

    monkeypatch.setattr(passwords, "verify_and_update", verify_then_end_sessions)
    r = c.post("/api/auth/password", headers=ORIGIN,
               json={"current_password": PASSWORD, "new_password": "a brand new secret"})
    monkeypatch.setattr(passwords, "verify_and_update", real)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "account_changed"
    assert sign_in(client(hub)).status_code == 200  # the password did not change
    assert sign_in(client(hub), password="a brand new secret").status_code == 401
