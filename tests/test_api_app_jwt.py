"""Machine callers with their own JWT (an ``apps:`` entry with ``api:``): one
fixed account, required claims, a key's operation rules, no approvals, and
the app checked again while work runs."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from hubzoid import auth
from hubzoid.access.session import require_same_origin
from hubzoid.auth import routes, sessions
from hubzoid.embed import config, tokens
from hubzoid.embed.config import AppsError

from tests.test_auth_routes import hub, person  # noqa: F401 - fixture

ISS, AUD = "https://token.actions.githubusercontent.com", "https://hub.example.com"


def _apps(hub, key, *, extra_api: str = "", enabled: bool = True, ops: str = "[chat, read]"):
    pem = key.as_pem(private=False).decode()
    indented = "\n".join("      " + line for line in pem.strip().splitlines())
    (hub.parent / "hubzoid.yaml").write_text(f"""version: 1
hubs: [sales]
apps:
  ci:
    mode: jwt
    enabled: {"true" if enabled else "false"}
    issuer: {ISS}
    audience: {AUD}
    public_key: |
{indented}
    api:
      account: robot@example.com
      operations: {ops}
      require: {{repository: acme/reports}}
{extra_api}""")
    config.reset()
    tokens.reset_cache()


def _token(key, **claims):
    now = int(time.time())
    body = {"iss": ISS, "aud": AUD, "sub": "repo:acme/reports:ref:refs/heads/main",
            "repository": "acme/reports", "iat": now, "exp": now + 300}
    body.update(claims)
    return jwt.encode({"alg": "RS256"}, body, key)


def _client(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)

    @app.get("/api/chat/ping")
    def ping(request: Request):
        u = auth.require_user(request, hub)
        return {"who": u.email, "credential": u.credential, "id": u.credential_id, "automated": u.via_key}

    @app.post("/api/chat")
    def send(request: Request):
        require_same_origin(request)
        return {"who": auth.require_user(request, hub).email}

    @app.post("/portal/api/people/block")
    def block(request: Request):
        return {"who": auth.require_user(request, hub).email}

    @app.post("/api/requests/{rid}/decisions")
    def decide(rid: str, request: Request):
        return {"who": auth.require_user(request, hub).email}

    return TestClient(app)


@pytest.mark.parametrize("api,message", [
    ("{account: robot@example.com, operations: [chat]}", "require"),
    ("{account: robot@example.com, operations: [approve], require: {sub: x}}", "never approve"),
    ("{account: robot, operations: [chat], require: {sub: x}}", "email"),
    ("{account: robot@example.com, operations: [chat], require: {aud: x}}", "require.aud"),
])
def test_an_api_app_is_checked_when_read(api, message):
    raw = {"mode": "jwt", "issuer": ISS, "audience": AUD, "jwks_url": "https://x.example/jwks"}
    with pytest.raises(ValueError) as exc:
        config.parse({"ci": {**raw, "api": __import__("yaml").safe_load(api)}})
    assert message in str(exc.value)
    with pytest.raises(ValueError):  # an API app never embeds the chat
        config.parse({"ci": {**raw, "access": {"agents": ["sales"]},
                             "api": {"account": "r@x.com", "operations": ["chat"], "require": {"sub": "s"}}}})


def test_a_machine_token_acts_as_its_one_account(hub):
    key = RSAKey.generate_key(2048)
    _apps(hub, key)
    person(hub, email="robot@example.com")
    c = _client(hub)
    good = {"authorization": "Bearer " + _token(key)}
    me = c.get("/api/chat/ping", headers=good).json()
    assert me == {"who": "robot@example.com", "credential": "app_jwt", "id": "app:ci", "automated": True}
    assert c.post("/api/chat", headers=good).status_code == 200          # no Origin needed, like a key
    assert c.post("/portal/api/people/block", headers=good).status_code == 403   # no manage
    assert c.post("/api/requests/r1/decisions", headers=good).status_code == 403  # never decides
    other_repo = {"authorization": "Bearer " + _token(key, repository="someone/else")}
    r = c.get("/api/chat/ping", headers=other_repo)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "claims_not_allowed"
    expired = {"authorization": "Bearer " + _token(key, iat=int(time.time()) - 900, exp=int(time.time()) - 600)}
    assert c.get("/api/chat/ping", headers=expired).status_code == 401
    forged = {"authorization": "Bearer " + _token(RSAKey.generate_key(2048))}
    assert c.get("/api/chat/ping", headers=forged).status_code == 401
    both = TestClient(c.app, cookies={sessions.SESSION_COOKIE: "x"})
    assert both.get("/api/chat/ping", headers=good).status_code == 400


def test_the_account_must_exist_and_be_active(hub):
    key = RSAKey.generate_key(2048)
    _apps(hub, key)
    c = _client(hub)
    r = c.get("/api/chat/ping", headers={"authorization": "Bearer " + _token(key)})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_unavailable"


def test_an_embedding_apps_token_is_not_an_account_credential(hub):
    key = RSAKey.generate_key(2048)
    _apps(hub, key)
    assert tokens.api_app(hub, _token(key)) is not None
    with pytest.raises(AppsError) as exc:
        tokens.verify(hub, _token(key))  # an API app can't embed the chat
    assert exc.value.code == "unknown_app"
    assert tokens.api_app(hub, "hzp_abc") is None and tokens.api_app(hub, "a.b.c") is None


def test_removing_or_disabling_the_app_stops_running_work(hub):
    from hubzoid.access.evaluator import credential_live
    from hubzoid.access.run_identity import LIVE_CREDENTIALS

    key = RSAKey.generate_key(2048)
    assert "app_jwt" in LIVE_CREDENTIALS
    _apps(hub, key)
    assert credential_live(hub, "app_jwt", "app:ci")
    _apps(hub, key, enabled=False)
    assert not credential_live(hub, "app_jwt", "app:ci")
    (hub.parent / "hubzoid.yaml").write_text("version: 1\nhubs: [sales]\napps: {}\n")
    config.reset()
    assert not credential_live(hub, "app_jwt", "app:ci")


def test_an_unknown_jwt_is_never_the_local_owner_or_the_cookie(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH", "false")
    key = RSAKey.generate_key(2048)
    _apps(hub, key)
    c = _client(hub)
    stranger = jwt.encode({"alg": "RS256"}, {"iss": "https://other.example", "aud": AUD, "sub": "x",
                                             "iat": int(time.time()), "exp": int(time.time()) + 60},
                          RSAKey.generate_key(2048))
    assert c.get("/api/chat/ping").status_code == 200  # sign-in off: this machine is the owner
    assert c.get("/api/chat/ping", headers={"authorization": "Bearer " + stranger}).status_code == 401
