"""Personal API keys: shown once, limited by operations, agents, expiry and the
owner's live access, refused for sensitive changes, and ended by revoke-all."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hubzoid import auth
from hubzoid.access.session import require_same_origin
from hubzoid.auth import api_keys, routes, sessions, users

from tests.test_auth_mfa import hub  # noqa: F401 - fixture
from tests.test_auth_routes import ORIGIN, person, sign_in


def client(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)

    @app.get("/api/chat/ping")
    def chat_ping(request: Request):
        return {"who": auth.require_user(request, hub).email}

    @app.post("/api/chat")
    def chat_send(request: Request):
        require_same_origin(request)
        return {"who": auth.require_user(request, hub).email}

    @app.get("/v1/models")
    def models(request: Request):
        return {"who": auth.require_user(request, hub).email}

    @app.get("/portal/api/people")
    def people(request: Request):
        return {"who": auth.require_user(request, hub).email}

    @app.post("/portal/api/people/block")
    def block(request: Request):
        require_same_origin(request)
        return {"who": auth.require_user(request, hub).email}

    return TestClient(app)


def _make(c, **body) -> str:
    r = c.post("/api/me/api-keys", json={"name": "script", **body}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    assert r.json()["key"].startswith("hzp_") and "key" not in r.json()["api_key"]
    return r.json()["key"]


def bearer(key: str) -> dict:
    return {"authorization": f"Bearer {key}"}


def test_chat_key_reaches_chat_only(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    key = _make(c, preset="chat")
    script = TestClient(c.app)
    assert script.get("/api/chat/ping", headers=bearer(key)).json()["who"] == "ana@example.com"
    # The key signs in as the person, under their name (not the key's).
    me = script.get("/api/auth/session", headers=bearer(key)).json()["user"]
    assert me["name"] == "Ana"
    # A key is never sent by a browser on its own: no Origin is needed.
    assert script.post("/api/chat", headers=bearer(key)).status_code == 200
    assert script.get("/portal/api/people", headers=bearer(key)).status_code == 403
    assert script.post("/portal/api/people/block", headers=bearer(key)).status_code == 403


def test_read_and_manage_operations(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    read, full = _make(c, preset="read"), _make(c, preset="all")
    s = TestClient(c.app)
    assert s.get("/portal/api/people", headers=bearer(read)).status_code == 200
    assert s.post("/portal/api/people/block", headers=bearer(read)).status_code == 403
    assert s.post("/portal/api/people/block", headers=bearer(full)).status_code == 200


def test_keys_can_never_make_keys_or_change_mfa(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    full = _make(c, preset="all")
    s = TestClient(c.app)
    for method, path in (("post", "/api/me/api-keys"), ("post", "/api/me/mfa/enroll"),
                         ("post", "/api/auth/reauth"), ("delete", "/api/me/mfa"),
                         ("post", "/api/me/sessions/revoke-others")):
        r = getattr(s, method)(path, headers=bearer(full), **({"json": {}} if method == "post" else {}))
        assert r.status_code == 403, (path, r.text)


def test_unknown_key_never_falls_back(hub, monkeypatch):
    person(hub)
    c = client(hub)
    sign_in(c)
    s = TestClient(c.app)
    assert s.get("/api/chat/ping", headers=bearer("hzp_nope_nope")).status_code == 401
    # Not even to the local owner when sign-in is off.
    monkeypatch.setenv("HUBZOID_AUTH", "false")
    sessions.reset_cache()
    assert s.get("/api/chat/ping", headers=bearer("hzp_nope_nope")).status_code == 401


def test_cookie_and_key_together_are_refused(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    key = _make(c, preset="chat")
    r = c.get("/api/chat/ping", headers=bearer(key))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ambiguous_credentials"


def test_revoke_all_and_block_end_keys(hub):
    ana = person(hub)
    c = client(hub)
    sign_in(c)
    key = _make(c, preset="chat")
    s = TestClient(c.app)
    assert s.get("/api/chat/ping", headers=bearer(key)).status_code == 200
    users.revoke_credentials(hub, ana["id"])
    assert s.get("/api/chat/ping", headers=bearer(key)).status_code == 401
    listed = api_keys.list_for(hub, ana["id"])
    assert listed[0]["state"] == "revoked"


def test_a_key_made_while_sign_in_was_off_stops_when_it_is_on(hub, monkeypatch):
    # With sign-in off anyone who reached the server was the local owner and
    # could make a key without proof; nobody can sign in as that owner later.
    monkeypatch.setenv("HUBZOID_AUTH", "false")
    sessions.reset_cache()
    c = client(hub)
    local = TestClient(c.app, base_url="http://localhost")
    made = local.post("/api/me/api-keys", json={"name": "laptop", "preset": "all"},
                      headers={"origin": "http://localhost"})
    assert made.status_code == 201, made.text
    key = made.json()["key"]
    assert local.get("/api/chat/ping", headers=bearer(key)).status_code == 200
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    sessions.reset_cache()
    hosted = TestClient(c.app, base_url="https://team.example.com")
    assert hosted.get("/api/chat/ping", headers=bearer(key)).status_code == 401


def test_revoke_one_key(hub):
    ana = person(hub)
    c = client(hub)
    sign_in(c)
    key = _make(c, preset="chat")
    kid = api_keys.list_for(hub, ana["id"])[0]["id"]
    assert c.delete(f"/api/me/api-keys/{kid}", headers=ORIGIN).status_code == 204
    assert TestClient(c.app).get("/api/chat/ping", headers=bearer(key)).status_code == 401


def test_agent_limited_keys_chat_only_with_those_agents(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    bad = c.post("/api/me/api-keys", json={"name": "x", "preset": "all", "hubs": ["sales"]},
                 headers=ORIGIN)
    assert bad.status_code == 422
    other = _make(c, preset="chat", hubs=["finance"])
    mine = _make(c, preset="chat", hubs=["sales"])
    s = TestClient(c.app)
    assert s.get("/v1/models", headers=bearer(mine)).status_code == 200
    assert s.get("/v1/models", headers=bearer(other)).status_code == 403
    # Conversations span agents, so a limited key uses the chat API (/v1) only.
    assert s.get("/api/chat/ping", headers=bearer(mine)).status_code == 403


def test_expired_key(hub, monkeypatch):
    ana = person(hub)
    c = client(hub)
    sign_in(c)
    key = _make(c, preset="chat", expires_in_days=1)
    later = api_keys.time.time() + 2 * 86400
    monkeypatch.setattr(api_keys.time, "time", lambda: later)
    assert TestClient(c.app).get("/api/chat/ping", headers=bearer(key)).status_code == 401
    assert api_keys.list_for(hub, ana["id"])[0]["state"] == "expired"


@pytest.mark.parametrize("ops,method,path,ok", [
    (("chat",), "POST", "/api/chat", True),
    (("chat",), "PATCH", "/api/conversations/c1", True),
    (("chat",), "PUT", "/api/chat/config", False),       # workspace chat settings
    (("chat",), "POST", "/api/chat/imports", False),
    (("manage",), "PUT", "/api/chat/config", True),
    (("chat",), "POST", "/v1/chat/completions", True),
    (("chat",), "GET", "/portal/api/people", False),
    (("read",), "GET", "/portal/api/people", True),
    (("read",), "POST", "/api/chat/abc", False),
    (("manage",), "POST", "/portal/api/change-requests/r1/confirm", False),
    (("manage",), "DELETE", "/api/me/api-keys/x", False),
    (("manage",), "GET", "/api/me/api-keys", True),
])
def test_operation_matrix(ops, method, path, ok):
    assert api_keys.allows(ops, method, path) is ok
