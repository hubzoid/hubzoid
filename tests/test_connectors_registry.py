"""The connector registry and its HTTP API: validation, secrecy, who may change
it, same-origin mutations, audit rows, capabilities, and people's list of
connections. No provider is contacted except where a test says so."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from hubzoid import capabilities, connectors
from hubzoid.access import store_for
from hubzoid.connectors import ConnectorError, registry, tokens
from tests import connectors_fakes as f

APP = "http://127.0.0.1:3599"
SAME = {"origin": APP}
ALICE, BOB, CAROL = "alice@example.org", "bob@example.org", "carol@example.org"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    f.clean_env(monkeypatch)
    return f.make_hub(tmp_path, "sales")


@pytest.fixture
def tc(hub):
    return f.client_for(f.app_for(hub), APP)


def body(**extra):
    return {"name": "Gmail", "url": "https://gmail-mcp.example.org/mcp", **extra}


def detail(r):
    return r.json()["detail"]


# ---------------------------------------------------------------------------
# Files are the definitions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("change, code", [
    ({"name": "x" * 81}, "invalid_name"),
    ({"url": "http://gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "ftp://gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "https://user:pw@gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "not a url"}, "invalid_url"),
    ({"id": "9mail"}, "invalid_id"),
    ({"auth_type": "basic"}, "invalid_auth_type"),
    ({"scopes": "a\"b"}, "invalid_scopes"),
    ({"tool_allowlist": ["ok", "no spaces"]}, "invalid_tool_allowlist"),
    ({"enabled": "yes"}, "invalid_enabled"),
])
def test_invalid_connector_files_are_refused(hub, change, code):
    with pytest.raises(ConnectorError) as err:
        f.define(hub, body(**change))
    assert err.value.code == code and err.value.message


def test_settings_listing_never_shows_a_secret(hub, tc, monkeypatch):
    secret = "pre-registered-secret-value"
    f.define(hub, body(client_id="hubzoid-app", client_secret=secret, scopes="mail.read, mail.read",
                       tool_allowlist="search_threads, get_thread"))
    listing = tc.get("/portal/api/connectors")
    assert listing.status_code == 200 and secret not in listing.text
    (c,) = listing.json()["connectors"]
    assert c["has_client_secret"] is True and c["scopes"] == "mail.read"
    assert c["tool_allowlist"] == ["search_threads", "get_thread"]
    assert registry.client_secret(hub, "gmail") == secret          # from the environment it names
    assert "env:" in (hub / "connectors" / "gmail.yaml").read_text() and secret not in (
        hub / "connectors" / "gmail.yaml").read_text()


def test_a_broken_edit_keeps_the_last_good_definition(hub):
    f.define(hub, body())
    assert registry.get(hub, "gmail").url == "https://gmail-mcp.example.org/mcp"
    (hub / "connectors" / "gmail.yaml").write_text("version: 1\nurl: ftp://nope\n")
    assert registry.get(hub, "gmail").url == "https://gmail-mcp.example.org/mcp"
    assert any("gmail.yaml" in p for p in registry.problems(hub))


def _connect(hub, cid, *, user_id="local-owner", email=f.OWNER, url="https://gmail-mcp.example.org/mcp"):
    tokens.store(hub, user_id=user_id, email=email, connector_id=cid,
                 token={"v": 1, "kind": "oauth", "access_token": "AT", "refresh_token": "RT",
                        "expires_at": None, "url": url})


def test_listing_counts_connections(hub, tc):
    f.define(hub, body())
    _connect(hub, "gmail")
    (c,) = tc.get("/portal/api/connectors").json()["connectors"]
    assert c["connections"] == 1 and c["dynamic_client"] is False


# ---------------------------------------------------------------------------
# Who may change the registry
# ---------------------------------------------------------------------------
def test_the_local_owner_administers_when_sign_in_is_off(tc):
    assert tc.get("/portal/api/connectors").status_code == 200


def test_capabilities_come_from_the_registry(hub, tmp_path, monkeypatch):
    f.define(hub, body())
    f.define(hub, body(name="Docs", url="https://docs.example.org/mcp", auth_type="none",
                              enabled=False))
    f.define(hub, body(name="HR", url="https://hr.example.org/mcp"))
    for cid in ("gmail", "docs"):                    # HR is registered but not offered here
        f.offer(hub, cid)
    rows = {r["permission"]: r for r in capabilities.catalog(hub)}
    assert "connector_hr" not in rows
    gmail, docs = rows["connector_gmail"], rows["connector_docs"]
    assert gmail["label"] == "Connect Gmail" and gmail["sensitive"] is True
    assert gmail["group"] == "connectors" and gmail["available"] is True
    assert docs["available"] is False and docs["status"] == "Switched off"
    # Open WebUI mode: the same Console connectors.
    rows = {r["permission"] for r in capabilities.catalog(hub)}
    assert "connector_gmail" in rows and "connector_hr" not in rows


def test_a_deleted_connector_leaves_its_grants_visible_as_obsolete(hub):
    f.define(hub, body())
    store_for(hub).grant(ALICE, "sales", "connector_gmail", actor="test")
    f.remove(hub, "gmail")
    rows = {r["permission"]: r for r in capabilities.catalog(hub, granted=["connector_gmail"])}
    assert rows["connector_gmail"]["obsolete"] is True


# ---------------------------------------------------------------------------
# People: their connections
# ---------------------------------------------------------------------------
def test_people_see_switched_on_connectors_and_their_own_state(hub, tc, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "user"), BOB: ("u-b", "user")})
    f.define(hub, body())
    f.define(hub, body(name="Old", url="https://old.example.org/mcp", enabled=False),
                    actor="test")
    f.define(hub, body(name="Gone", url="https://gone.example.org/mcp", enabled=False),
                    actor="test")
    _connect(hub, "old", user_id="u-a", email=ALICE, url="https://old.example.org/mcp")
    assert tc.get("/api/connections").status_code == 401
    mine = tc.get("/api/connections", headers={"x-test-user": ALICE}).json()
    assert [(c["connector_id"], c["connected"], c["enabled"]) for c in mine] == [
        ("gmail", False, True), ("old", True, False)]
    assert mine[0]["status"] == "none" and mine[0]["connected_at"] is None
    assert mine[1]["status"] == "ok" and mine[1]["allowed"] is False
    theirs = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert [c["connector_id"] for c in theirs] == ["gmail"]


def test_allowed_follows_the_connector_capability(hub, tc, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "admin"), BOB: ("u-b", "user")})
    f.define(hub, body())
    gs = store_for(hub)
    gs.bootstrap([ALICE])
    mine = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert mine[0]["allowed"] is False
    r = tc.post("/api/connections/gmail/connect", json={}, headers={**SAME, "x-test-user": BOB})
    assert r.status_code == 403 and detail(r)["code"] == "not_allowed"
    gs.grant(BOB, "sales", "connector_gmail", actor="test")
    # Held, but no agent offers it yet.
    assert tc.get("/api/connections", headers={"x-test-user": BOB}).json()[0]["allowed"] is False
    f.offer(hub, "gmail")
    mine = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert mine[0]["allowed"] is True
    gs.suspend(BOB, actor="test")
    assert tc.get("/api/connections", headers={"x-test-user": BOB}).json()[0]["allowed"] is False


def test_connect_refusals(hub, tc):
    f.define(hub, body(enabled=False))
    r = tc.post("/api/connections/gmail/connect", json={}, headers=SAME)
    assert r.status_code == 409 and detail(r)["code"] == "disabled"
    r = tc.post("/api/connections/nope/connect", json={}, headers=SAME)
    assert r.status_code == 404
    f.change(hub, "gmail", {"enabled": True})
    for bad in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)", "\\\\evil"):
        r = tc.post("/api/connections/gmail/connect", json={"return_to": bad}, headers=SAME)
        assert r.status_code == 422 and detail(r)["code"] == "invalid_return_to", bad
    r = tc.post("/api/connections/gmail/connect", json=["x"], headers=SAME)
    assert r.status_code == 422


def test_an_unreachable_server_fails_clearly(hub, tc):
    port = f.free_port()
    f.define(hub, body(url=f"http://127.0.0.1:{port}/mcp"))
    f.grant_connector(hub, "gmail")
    r = tc.post("/api/connections/gmail/connect", json={}, headers=SAME)
    assert r.status_code == 502 and detail(r)["code"] == "unreachable"
    r = tc.post("/portal/api/connectors/gmail/test", headers=SAME)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["error"]["code"] == "unreachable"


def test_a_connector_without_sign_in_connects_at_once(hub, tc):
    with f.bearer_mcp("docs", {}, {"lookup": lambda who: "found"}) as url:
        f.define(hub, body(name="Docs", url=url, auth_type="none"))
        f.grant_connector(hub, "docs")
        r = tc.post("/portal/api/connectors/docs/test", headers=SAME)
        assert r.json()["ok"] is True and r.json()["requires_auth"] is False
        # The Test lists what the server offers, for the Console and its tool picker.
        assert [t["name"] for t in r.json()["tools"]] == ["lookup"]
        r = tc.post("/api/connections/docs/connect", json={}, headers=SAME)
        assert r.json() == {"authorize_url": "/account/connections?connected=docs"}
        r = tc.post("/api/connections/docs/connect", json={"return_to": "/c/abc"}, headers=SAME)
        assert r.json() == {"authorize_url": "/c/abc"}
        assert tc.get("/api/connections").json()[0]["connected"] is True
        assert tc.delete("/api/connections/docs", headers=SAME).status_code == 204
        assert tc.get("/api/connections").json()[0]["connected"] is False
        # Disconnecting twice is fine.
        assert tc.delete("/api/connections/docs", headers=SAME).status_code == 204


def test_a_shared_key_connector_has_nothing_to_connect_and_starts_no_sign_in(hub, tc, monkeypatch):
    from hubzoid.connectors import oauth_flow

    f.define(hub, body(name="Team", url="https://team.example.org/mcp", auth_type="shared",
                              shared_secret="k-1"))
    f.grant_connector(hub, "team")
    started = []
    monkeypatch.setattr(oauth_flow, "start", lambda *a, **k: started.append(a) or "/x")
    for path in ("/api/connections/team/connect", "/portal/api/connections/team/connect"):
        r = tc.post(path, json={}, headers=SAME)
        assert r.status_code == 409 and detail(r)["code"] == "shared", path
    assert started == []


def test_a_server_that_needs_sign_in_is_reported_for_a_no_auth_connector(hub, tc):
    with f.bearer_mcp("private", {"tok": "x"}, {"lookup": lambda who: who}) as url:
        f.define(hub, body(name="Private", url=url, auth_type="none"))
        r = tc.post("/portal/api/connectors/private/test", headers=SAME).json()
        assert r["ok"] is False and r["requires_auth"] is True
        assert r["error"]["code"] == "requires_auth" and "tools" not in r


def test_the_web_app_mounts_the_routes_before_the_console_files(hub):
    """webapp.mount runs before portal.mount_portal, whose static files at
    /portal would otherwise answer /portal/api/connectors."""
    from fastapi import FastAPI

    from hubzoid import portal, webapp

    app = FastAPI()
    webapp.mount(app, hub)
    portal.mount_portal(app, hub)
    c = f.client_for(app, APP)
    r = c.get("/portal/api/connectors")
    assert r.status_code == 200 and r.json() == {"connectors": []}
    r = c.get("/oauth/connectors/gmail/callback?state=x&code=y")
    assert r.status_code == 302 and r.headers["location"].endswith("error=invalid_state")
    assert c.get("/api/connections").json() == []


def test_sign_in_off_answers_only_requests_to_this_machine(hub, monkeypatch):
    """Every request is the owner when sign-in is off, so a page using DNS
    rebinding (Host: its own name) must not reach these routes."""
    rebound = f.client_for(f.app_for(hub), "http://rebind.example:3599")
    evil = {"origin": "http://rebind.example:3599"}
    for r in (rebound.get("/portal/api/connectors"), rebound.get("/api/connections"),
              rebound.post("/portal/api/connectors", json=body(), headers=evil),
              rebound.post("/api/connections/gmail/connect", json={}, headers=evil)):
        assert r.status_code == 403 and detail(r)["code"] == "untrusted_host"
    assert registry.list_all(hub) == []
    # An operator who configured the public address may use it.
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "http://rebind.example:3599")
    assert rebound.get("/portal/api/connectors").status_code == 200


def test_a_connect_needs_a_trusted_redirect_origin(hub, monkeypatch):
    """Sign-in on but no public address configured: a redirect URI is never
    built on a Host someone sent."""
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "user")})
    f.define(hub, body())
    f.grant_connector(hub, "gmail", ALICE)
    c = f.client_for(f.app_for(hub), "https://hub.example.org")
    r = c.post("/api/connections/gmail/connect", json={},
               headers={"origin": "https://hub.example.org", "x-test-user": ALICE})
    assert r.status_code == 409 and detail(r)["code"] == "public_url_required"


def test_oauth_callback_queries_never_reach_the_access_log(hub):
    import logging

    from hubzoid.connectors import routes

    routes.mount(__import__("fastapi").FastAPI(), hub)
    routes.mount(__import__("fastapi").FastAPI(), hub)  # installed once
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(x, routes.RedactOAuthQuery) for x in access.filters) == 1

    def line(path):
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:5", "GET", path, "1.1", 302), None)
        # Every filter, in order, as the logger applies them (other modules,
        # such as hubzoid.auth.logredact, may have installed theirs after ours).
        assert all(flt.filter(record) for flt in list(access.filters))
        return record.getMessage()

    shown = line("/oauth/connectors/gmail/callback?code=SECRET-CODE&state=SECRET-STATE")
    assert "SECRET" not in shown and "/oauth/connectors/gmail/callback?[redacted]" in shown
    assert "/api/connections?x=1" in line("/api/connections?x=1")


