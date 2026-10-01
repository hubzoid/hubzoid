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
# Validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("change, code", [
    ({"name": ""}, "invalid_name"),
    ({"name": "x" * 81}, "invalid_name"),
    ({"url": "http://gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "ftp://gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "https://user:pw@gmail-mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "https://gmail-mcp.example.org/mcp#frag"}, "invalid_url"),
    ({"url": "https://gmail mcp.example.org/mcp"}, "invalid_url"),
    ({"url": "https://example.org:99999/mcp"}, "invalid_url"),
    ({"url": "not a url"}, "invalid_url"),
    ({"id": "Gmail!"}, "invalid_id"),
    ({"id": "9mail"}, "invalid_id"),
    ({"id": "g"}, "invalid_id"),
    ({"auth_type": "basic"}, "invalid_auth_type"),
    ({"scopes": "a\"b"}, "invalid_scopes"),
    ({"scopes": 7}, "invalid_scopes"),
    ({"tool_allowlist": ["ok", "no spaces"]}, "invalid_tool_allowlist"),
    ({"enabled": "yes"}, "invalid_enabled"),
    ({"client_secret": "s3cret"}, "invalid_client_secret"),
    ({"nickname": "x"}, "invalid_request"),
])
def test_invalid_connectors_are_refused(tc, change, code):
    r = tc.post("/portal/api/connectors", json=body(**change), headers=SAME)
    assert r.status_code == 422, r.text
    assert detail(r)["code"] == code and detail(r)["message"]


def test_loopback_http_is_allowed_for_local_development(tc):
    for i, url in enumerate(("http://127.0.0.1:3555/mcp", "http://localhost:9/mcp",
                             "http://[::1]:9/mcp")):
        r = tc.post("/portal/api/connectors", json=body(id=f"local{i}", url=url), headers=SAME)
        assert r.status_code == 201, r.text


def test_ids_come_from_names_and_stay_unique(hub, tc):
    first = tc.post("/portal/api/connectors", json=body(name="Google Drive"), headers=SAME).json()
    second = tc.post("/portal/api/connectors", json=body(name="Google Drive"), headers=SAME).json()
    assert first["connector"]["id"] == "google_drive"
    assert second["connector"]["id"] == "google_drive_2"
    r = tc.post("/portal/api/connectors", json=body(id="google_drive"), headers=SAME)
    assert r.status_code == 409 and detail(r)["code"] == "exists"
    r = tc.post("/portal/api/connectors", json=body(name="42"), headers=SAME)
    assert r.status_code == 201 and r.json()["connector"]["id"] == "c_42"


def test_fields_are_normalised(tc):
    r = tc.post("/portal/api/connectors", json=body(
        name="  Team   Gmail ", scopes="mail.read, mail.send mail.read",
        tool_allowlist="search_threads, get_thread", enabled=False), headers=SAME)
    c = r.json()["connector"]
    assert c["name"] == "Team Gmail" and c["scopes"] == "mail.read mail.send"
    assert c["tool_allowlist"] == ["search_threads", "get_thread"]
    assert c["enabled"] is False and c["auth_type"] == "oauth"
    assert c["capability"] == "connector_team_gmail"
    assert c["redirect_uri"] == f"{APP}/oauth/connectors/team_gmail/callback"


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def test_client_secrets_are_encrypted_and_never_returned(hub, tc):
    secret = "pre-registered-secret-value"
    r = tc.post("/portal/api/connectors", json=body(client_id="hubzoid-app", client_secret=secret),
                headers=SAME)
    assert r.status_code == 201
    assert secret not in r.text and r.json()["connector"]["has_client_secret"] is True
    assert secret not in tc.get("/portal/api/connectors").text
    r = tc.patch("/portal/api/connectors/gmail", json={"name": "Mail"}, headers=SAME)
    assert secret not in r.text and r.json()["connector"]["has_client_secret"] is True
    with connectors.engine(hub).connect() as conn:
        assert secret not in str(conn.execute(text("SELECT * FROM hz_connectors")).fetchall())
    assert registry.client_secret(hub, "gmail") == secret
    for c in registry.list_all(hub):
        assert secret not in str(c.public()) and secret not in repr(c)


def test_patch_keeps_clears_and_ties_the_secret_to_its_client(hub, tc):
    tc.post("/portal/api/connectors", json=body(client_id="app-1", client_secret="s1"), headers=SAME)
    # Omitted: kept.
    tc.patch("/portal/api/connectors/gmail", json={"scopes": "a b"}, headers=SAME)
    assert registry.client_secret(hub, "gmail") == "s1"
    # A new client ID without a secret drops the old one's secret.
    r = tc.patch("/portal/api/connectors/gmail", json={"client_id": "app-2"}, headers=SAME)
    assert r.json()["connector"]["has_client_secret"] is False
    tc.patch("/portal/api/connectors/gmail", json={"client_secret": "s2"}, headers=SAME)
    assert registry.client_secret(hub, "gmail") == "s2"
    # Null clears.
    r = tc.patch("/portal/api/connectors/gmail", json={"client_secret": None}, headers=SAME)
    assert r.json()["connector"]["has_client_secret"] is False
    # A secret needs its client ID.
    r = tc.patch("/portal/api/connectors/gmail", json={"client_id": None, "client_secret": "s3"},
                 headers=SAME)
    assert r.status_code == 422 and detail(r)["code"] == "invalid_client_secret"
    # No sign-in: no client at all.
    r = tc.patch("/portal/api/connectors/gmail", json={"auth_type": "none"}, headers=SAME)
    c = r.json()["connector"]
    assert c["client_id"] is None and c["has_client_secret"] is False


def test_ids_cannot_change_and_unknown_ids_are_404(tc):
    tc.post("/portal/api/connectors", json=body(), headers=SAME)
    r = tc.patch("/portal/api/connectors/gmail", json={"id": "mail"}, headers=SAME)
    assert r.status_code == 422 and detail(r)["code"] == "invalid_id"
    assert tc.patch("/portal/api/connectors/nope", json={}, headers=SAME).status_code == 404
    assert tc.delete("/portal/api/connectors/nope", headers=SAME).status_code == 404
    assert tc.post("/portal/api/connectors/nope/test", headers=SAME).status_code == 404
    assert tc.patch("/portal/api/connectors/Bad!", json={}, headers=SAME).status_code == 404


def _connect(hub, cid, *, user_id="local-owner", email=f.OWNER, url="https://gmail-mcp.example.org/mcp"):
    tokens.store(hub, user_id=user_id, email=email, connector_id=cid,
                 token={"v": 1, "kind": "oauth", "access_token": "AT", "refresh_token": "RT",
                        "expires_at": None, "url": url})


def test_a_new_url_or_sign_in_drops_existing_connections(hub, tc):
    tc.post("/portal/api/connectors", json=body(), headers=SAME)
    _connect(hub, "gmail")
    tc.patch("/portal/api/connectors/gmail", json={"name": "Mail", "scopes": "x"}, headers=SAME)
    assert tokens.get(hub, "local-owner", "gmail") is not None  # nothing that matters changed
    r = tc.patch("/portal/api/connectors/gmail", json={"url": "https://other.example.org/mcp"},
                 headers=SAME)
    assert r.status_code == 200
    assert tokens.get(hub, "local-owner", "gmail") is None
    _connect(hub, "gmail", url="https://other.example.org/mcp")
    tc.patch("/portal/api/connectors/gmail", json={"auth_type": "none"}, headers=SAME)
    assert tokens.get(hub, "local-owner", "gmail") is None


def test_delete_removes_connections_and_authorizations(hub, tc):
    tc.post("/portal/api/connectors", json=body(), headers=SAME)
    _connect(hub, "gmail")
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_connector_flows (state, user_id, connector_id, "
                          "payload_enc, created_at, expires_at) VALUES ('s', 'u', 'gmail', 'x', 0, 9e9)"))
    assert tc.delete("/portal/api/connectors/gmail", headers=SAME).status_code == 204
    with connectors.engine(hub).connect() as conn:
        for table in ("hz_connectors", "hz_connector_tokens", "hz_connector_flows"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_listing_counts_connections(hub, tc):
    tc.post("/portal/api/connectors", json=body(), headers=SAME)
    _connect(hub, "gmail")
    (c,) = tc.get("/portal/api/connectors").json()["connectors"]
    assert c["connections"] == 1 and c["dynamic_client"] is False


# ---------------------------------------------------------------------------
# Who may change the registry
# ---------------------------------------------------------------------------
def test_only_organization_administrators_manage_connectors(hub, tc, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "admin"), BOB: ("u-b", "admin"),
                                  CAROL: ("u-c", "user")})
    gs = store_for(hub)
    gs.bootstrap([ALICE], authoritative=True)
    gs.grant(BOB, "sales", "manage_access", actor="test")  # an agent administrator only
    assert tc.get("/portal/api/connectors").status_code == 401
    assert detail(tc.get("/portal/api/connectors"))["code"] == "unauthenticated"
    for who in (BOB, CAROL):
        r = tc.get("/portal/api/connectors", headers={"x-test-user": who})
        assert r.status_code == 403 and detail(r)["code"] == "forbidden", who
        r = tc.post("/portal/api/connectors", json=body(), headers={**SAME, "x-test-user": who})
        assert r.status_code == 403, who
    r = tc.get("/portal/api/connectors", headers={"x-test-user": ALICE})
    assert r.status_code == 200 and r.json() == {"connectors": []}
    r = tc.post("/portal/api/connectors", json=body(), headers={**SAME, "x-test-user": ALICE})
    assert r.status_code == 201 and r.json()["connector"]["created_by"] == ALICE


def test_the_local_owner_administers_when_sign_in_is_off(tc):
    assert tc.get("/portal/api/connectors").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.example"}, {"origin": "null"},
                                     {"referer": "https://evil.example/page"}])
def test_mutations_need_the_same_origin(hub, tc, headers):
    assert tc.post("/portal/api/connectors", json=body(), headers=headers).status_code == 403
    r = tc.post("/portal/api/connectors", json=body(), headers=headers)
    assert detail(r)["code"] == "cross_origin"
    registry.create(hub, body(), actor="test")
    for method, path in (("patch", "/portal/api/connectors/gmail"),
                         ("delete", "/portal/api/connectors/gmail"),
                         ("post", "/portal/api/connectors/gmail/test"),
                         ("post", "/api/connections/gmail/connect"),
                         ("delete", "/api/connections/gmail")):
        kwargs = {"json": {}} if method in ("patch", "post") else {}
        r = getattr(tc, method)(path, headers=headers, **kwargs)
        assert r.status_code == 403 and detail(r)["code"] == "cross_origin", path
    assert registry.get(hub, "gmail") is not None


def test_changes_are_audited_like_console_changes(hub, tc):
    tc.post("/portal/api/connectors", json=body(), headers=SAME)
    tc.patch("/portal/api/connectors/gmail", json={"enabled": False}, headers=SAME)
    tc.delete("/portal/api/connectors/gmail", headers=SAME)
    rows = store_for(hub).read_access_audit(10, hubs=["*"])
    got = [(r["actor"], r["action"], r["hub"], r["permission"]) for r in rows]
    assert got == [(f.OWNER, a, "*", "connector_gmail")
                   for a in ("connector_delete", "connector_update", "connector_create")]
    with connectors.engine(hub).connect() as conn:
        surfaces = {r[0] for r in conn.execute(text(
            "SELECT surface FROM hz_access_audit WHERE action LIKE 'connector_%'"))}
    assert surfaces == {"console"}


# ---------------------------------------------------------------------------
# Capabilities: connector_<id> comes from the registry in the default mode
# ---------------------------------------------------------------------------
def test_capabilities_come_from_the_registry(hub, tmp_path, monkeypatch):
    registry.create(hub, body(), actor="test")
    registry.create(hub, body(name="Docs", url="https://docs.example.org/mcp", auth_type="none",
                              enabled=False), actor="test")
    # An Open WebUI server is not offered in the default mode.
    from tests import connect_helpers as h

    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[], secret="s", servers=[
        {"id": "notion", "name": "Notion", "url": "https://notion.example.org/mcp"}])
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("OWUI_NATIVE_MCP", "true")
    rows = {r["permission"]: r for r in capabilities.catalog(hub)}
    assert "connector_notion" not in rows
    gmail, docs = rows["connector_gmail"], rows["connector_docs"]
    assert gmail["label"] == "Connect Gmail" and gmail["sensitive"] is True
    assert gmail["group"] == "tools" and gmail["available"] is True
    assert docs["available"] is False and docs["status"] == "Switched off"
    # The legacy mode is unchanged: Open WebUI's servers, not the registry.
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    rows = {r["permission"] for r in capabilities.catalog(hub)}
    assert "connector_notion" in rows and "connector_gmail" not in rows


def test_a_deleted_connector_leaves_its_grants_visible_as_obsolete(hub):
    registry.create(hub, body(), actor="test")
    store_for(hub).grant(ALICE, "sales", "connector_gmail", actor="test")
    registry.delete(hub, "gmail", actor="test")
    rows = {r["permission"]: r for r in capabilities.catalog(hub, granted=["connector_gmail"])}
    assert rows["connector_gmail"]["obsolete"] is True


# ---------------------------------------------------------------------------
# People: their connections
# ---------------------------------------------------------------------------
def test_people_see_switched_on_connectors_and_their_own_state(hub, tc, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "user"), BOB: ("u-b", "user")})
    registry.create(hub, body(), actor="test")
    registry.create(hub, body(name="Old", url="https://old.example.org/mcp", enabled=False),
                    actor="test")
    registry.create(hub, body(name="Gone", url="https://gone.example.org/mcp", enabled=False),
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


def test_allowed_follows_the_connector_capability_on_managed_hubs(hub, tc, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "admin"), BOB: ("u-b", "user")})
    registry.create(hub, body(), actor="test")
    gs = store_for(hub)
    # Not yet managed in the Console: the surface rule only.
    mine = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert mine[0]["allowed"] is True
    gs.bootstrap([ALICE], authoritative=True)
    mine = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert mine[0]["allowed"] is False
    r = tc.post("/api/connections/gmail/connect", json={}, headers={**SAME, "x-test-user": BOB})
    assert r.status_code == 403 and detail(r)["code"] == "not_allowed"
    gs.grant(BOB, "sales", "connector_gmail", actor="test")
    mine = tc.get("/api/connections", headers={"x-test-user": BOB}).json()
    assert mine[0]["allowed"] is True
    gs.suspend(BOB, actor="test")
    assert tc.get("/api/connections", headers={"x-test-user": BOB}).json()[0]["allowed"] is False


def test_connect_refusals(hub, tc):
    registry.create(hub, body(enabled=False), actor="test")
    r = tc.post("/api/connections/gmail/connect", json={}, headers=SAME)
    assert r.status_code == 409 and detail(r)["code"] == "disabled"
    r = tc.post("/api/connections/nope/connect", json={}, headers=SAME)
    assert r.status_code == 404
    registry.update(hub, "gmail", {"enabled": True}, actor="test")
    for bad in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)", "\\\\evil"):
        r = tc.post("/api/connections/gmail/connect", json={"return_to": bad}, headers=SAME)
        assert r.status_code == 422 and detail(r)["code"] == "invalid_return_to", bad
    r = tc.post("/api/connections/gmail/connect", json=["x"], headers=SAME)
    assert r.status_code == 422


def test_an_unreachable_server_fails_clearly(hub, tc):
    port = f.free_port()
    registry.create(hub, body(url=f"http://127.0.0.1:{port}/mcp"), actor="test")
    r = tc.post("/api/connections/gmail/connect", json={}, headers=SAME)
    assert r.status_code == 502 and detail(r)["code"] == "unreachable"
    r = tc.post("/portal/api/connectors/gmail/test", headers=SAME)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["error"]["code"] == "unreachable"


def test_a_connector_without_sign_in_connects_at_once(hub, tc):
    with f.bearer_mcp("docs", {}, {"lookup": lambda who: "found"}) as url:
        registry.create(hub, body(name="Docs", url=url, auth_type="none"), actor="test")
        r = tc.post("/portal/api/connectors/docs/test", headers=SAME)
        assert r.json()["ok"] is True and r.json()["requires_auth"] is False
        r = tc.post("/api/connections/docs/connect", json={}, headers=SAME)
        assert r.json() == {"authorize_url": "/account/connections?connected=docs"}
        r = tc.post("/api/connections/docs/connect", json={"return_to": "/c/abc"}, headers=SAME)
        assert r.json() == {"authorize_url": "/c/abc"}
        assert tc.get("/api/connections").json()[0]["connected"] is True
        assert tc.delete("/api/connections/docs", headers=SAME).status_code == 204
        assert tc.get("/api/connections").json()[0]["connected"] is False
        # Disconnecting twice is fine.
        assert tc.delete("/api/connections/docs", headers=SAME).status_code == 204


def test_a_server_that_needs_sign_in_is_reported_for_a_no_auth_connector(hub, tc):
    with f.bearer_mcp("private", {"tok": "x"}, {"lookup": lambda who: who}) as url:
        registry.create(hub, body(name="Private", url=url, auth_type="none"), actor="test")
        r = tc.post("/portal/api/connectors/private/test", headers=SAME).json()
        assert r["ok"] is False and r["requires_auth"] is True
        assert r["error"]["code"] == "requires_auth"


def test_registry_errors_are_connector_errors(hub):
    with pytest.raises(ConnectorError) as err:
        registry.create(hub, "not a dict", actor="test")
    assert err.value.code == "invalid_request" and err.value.status == 422
    assert registry.get(hub, "../etc") is None
    assert registry.delete(hub, "../etc", actor="test") is False


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


def test_a_body_that_is_not_json_gets_the_api_error_shape(tc):
    r = tc.post("/portal/api/connectors", content=b"{not json", headers={
        **SAME, "content-type": "application/json"})
    assert r.status_code == 422 and detail(r)["code"] == "invalid_request"
    r = tc.post("/portal/api/connectors", headers=SAME)
    assert r.status_code == 422 and detail(r)["code"] == "invalid_request"


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
    registry.create(hub, body(), actor="test")
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
