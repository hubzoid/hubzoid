"""Console account operations in the default mode: ``HubzoidAccounts`` against
the ``AccountDirectory`` protocol, the access service handing out one-time
sign-in links, and the Console API on Hubzoid sessions."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from hubzoid import portal
from hubzoid.access import accounts as accountlib
from hubzoid.access import store_for
from hubzoid.access import Identity, identity_scope
from hubzoid.access.service import AccessService, Actor, Denied
from hubzoid.auth import links, passwords, routes, sessions, users
from hubzoid.tools import access_admin

ENV = (
    "HUBZOID_UI", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_ALLOWED_ORIGINS",
    "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD", "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD",
    "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_GATEWAY_ADMIN_PASSWORD", "HUBZOID_DEPLOYMENT",
    "DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_MERGE_ACCOUNTS_BY_EMAIL",
    "OAUTH_ALLOWED_DOMAINS", "HUBZOID_PORTAL_DEV", "HUBZOID_PORTAL_DEV_USER", "OWUI_INTERNAL_URL",
    "ENABLE_SIGNUP",
)
ORIGIN = {"origin": "http://testserver"}
OWNER = "owner@example.com"
OWNER_PASSWORD = "owner password 1"


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


@pytest.fixture
def owner(hub, monkeypatch):
    """The configured owner: an administrator with the owner's grants."""
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", OWNER)
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", OWNER_PASSWORD)
    user = users.bootstrap_admin_from_env(hub)
    assert store_for(hub).can(OWNER, "*", "manage_access")
    return user


def actor() -> Actor:
    return Actor(OWNER, "console", "session")


def live_sessions(hub, user_id) -> int:
    from hubzoid.auth.schema import engine_for

    with engine_for(hub).connect() as conn:
        return conn.execute(text("SELECT count(*) FROM hz_sessions WHERE user_id=:u AND "
                                 "revoked_at IS NULL"), {"u": user_id}).scalar()


# ---- HubzoidAccounts against the protocol -----------------------------------------------

def test_directory_is_hubzoid_accounts_in_default_mode(hub, monkeypatch):
    directory = accountlib.for_deployment(hub)
    assert isinstance(directory, accountlib.HubzoidAccounts) and directory.links
    for method in ("create", "update", "delete", "get", "find", "admins"):
        assert callable(getattr(directory, method))
    assert accountlib.configured(hub) is True
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "service@example.com")
    assert accountlib.service_account_email(hub) == ""  # no service account here
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    with pytest.raises(accountlib.AccountsUnavailable):
        accountlib.for_deployment(hub)  # legacy: Open WebUI and its service account
    assert accountlib.service_account_email(hub) == "service@example.com"
    assert accountlib.configured(hub) is False


def test_protocol_shapes_and_errors(hub):
    directory = accountlib.HubzoidAccounts(hub)
    created = directory.create(email="Ana@Example.com", name="Ana", password=None)
    assert set(created) == {"id", "email", "name", "role", "sign_in"}
    assert (created["email"], created["role"], created["sign_in"]) == ("ana@example.com", "user", "password")
    assert directory.get(created["id"]) == created
    assert directory.find("ANA@example.com") == created
    assert directory.get("missing") is None and directory.find("x@example.com") is None
    with pytest.raises(accountlib.AccountError) as exc:
        directory.create(email="ana@example.com", name="Again")
    assert (exc.value.status, exc.value.code) == (409, "account_exists")
    with pytest.raises(ValueError):
        directory.create(email="b@example.com", name="B", role="admin")
    with pytest.raises(accountlib.AccountError) as exc:
        directory.update("missing", name="X")
    assert exc.value.code == "not_found"
    with pytest.raises(accountlib.AccountError) as exc:
        directory.delete("missing")
    assert exc.value.code == "not_found"
    google = directory.create(email="g@example.com", name="G", password_enabled=False)
    assert google["sign_in"] == "google"


def test_updates_follow_the_account_rules(hub):
    directory = accountlib.HubzoidAccounts(hub)
    user = users.create(hub, email="p@example.com", name="P", status="pending")
    assert directory.get(user["id"])["role"] == "pending"
    directory.update(user["id"], role="user")  # approval
    assert users.get(hub, user["id"])["status"] == "active"
    sessions.create_session(hub, user, method="password")
    directory.update(user["id"], role="admin")
    assert directory.get(user["id"])["role"] == "admin" and live_sessions(hub, user["id"]) == 0
    assert directory.admins() == ["p@example.com"]
    sessions.create_session(hub, user, method="password")
    directory.update(user["id"], password="brand new secret", name="Pat")
    assert live_sessions(hub, user["id"]) == 0
    assert passwords.verify("brand new secret", users.store(hub).password_hash(user["id"]))
    assert directory.get(user["id"])["name"] == "Pat"
    directory.update(user["id"], role="pending")
    assert directory.get(user["id"])["role"] == "pending"
    with pytest.raises(accountlib.AccountError) as exc:
        directory.update(user["id"], password="short")
    assert exc.value.code == "invalid_password"
    directory.delete(user["id"])
    assert directory.get(user["id"]) is None


def test_links_are_relative_or_on_the_public_url(hub, monkeypatch):
    directory = accountlib.HubzoidAccounts(hub)
    user = directory.create(email="l@example.com", name="L")
    out = directory.issue_link(user["id"], created_by=OWNER)
    assert out["link"].startswith("/auth/set-password?token=") and out["expires_at"] > 0
    token = out["link"].split("token=", 1)[1]
    assert links.inspect(hub, token)["valid"]
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/")
    again = directory.issue_link(user["id"])
    assert again["link"].startswith("https://hub.example.com/auth/set-password?token=")
    assert not links.inspect(hub, token)["valid"]  # the newer link replaced it


def test_reset_with_link_ends_the_old_password_and_sessions(hub):
    directory = accountlib.HubzoidAccounts(hub)
    user = users.create(hub, email="r@example.com", name="R", password="old password 1")
    sessions.create_session(hub, user, method="password")
    out = directory.reset_with_link(user["id"], created_by=OWNER)
    assert users.store(hub).password_hash(user["id"]) is None
    assert live_sessions(hub, user["id"]) == 0
    assert links.inspect(hub, out["link"].split("token=")[1])["purpose"] == "reset_password"


def test_sign_in_options_in_default_mode(hub, monkeypatch):
    assert accountlib.sign_in_options(hub) == {"password": True, "google": False, "links": True}
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    assert accountlib.sign_in_options(hub)["google"] is False  # not without merging by email
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "example.com")
    assert accountlib.sign_in_options(hub) == {"password": True, "google": True, "links": True,
                                               "google_domains": ["example.com"]}


# ---- the access service in link mode ----------------------------------------------------

def test_add_user_returns_a_one_time_link(hub, owner):
    service = AccessService(hub)
    assert service.links_mode()
    out = service.create_account(actor(), email="new@example.com", name="New",
                                  grants=[("sales", "use_hub")])
    assert out["link"].startswith("/auth/set-password?token=") and out["expires_at"]
    assert out["sign_in"] == "password" and out["grants"] == {"sales": ["use_hub"]}
    user = users.find_by_email(hub, "new@example.com")
    assert user["password_enabled"] and not user["has_password"]
    gs = store_for(hub)
    assert gs.identity("new@example.com")["owui_id"] == user["id"]
    assert gs.can("new@example.com", "sales", "use_hub")
    # The person uses the link: password set, signed in.
    app = FastAPI()
    routes.mount(app, hub)
    c = TestClient(app)
    token = out["link"].split("token=", 1)[1]
    r = c.post(f"/api/auth/link/{token}", json={"password": "their own secret"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"]["email"] == "new@example.com"


def test_add_user_with_a_typed_password_or_google(hub, owner, monkeypatch):
    service = AccessService(hub)
    out = service.create_account(actor(), email="typed@example.com", name="T",
                                 password="typed by admin", grants=[])
    assert "link" not in out
    assert passwords.verify("typed by admin", users.store(hub).password_hash(
        users.find_by_email(hub, "typed@example.com")["id"]))
    with pytest.raises(Denied) as exc:
        service.create_account(actor(), email="g@example.com", name="G", sign_in="google", grants=[])
    assert exc.value.code == "google_unavailable"
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    out = service.create_account(actor(), email="g@example.com", name="G", sign_in="google",
                                 grants=[])
    assert "link" not in out and out["sign_in"] == "google"
    assert not users.find_by_email(hub, "g@example.com")["password_enabled"]
    with pytest.raises(Denied) as exc:
        service.create_account(actor(), email="short@example.com", name="S", password="short",
                               grants=[])
    assert exc.value.code == "invalid_password"


def test_add_user_refuses_an_existing_account(hub, owner):
    service = AccessService(hub)
    service.create_account(actor(), email="dup@example.com", name="D", grants=[])
    with pytest.raises(Denied) as exc:
        service.create_account(actor(), email="dup@example.com", name="D", grants=[])
    assert exc.value.code == "account_exists"


def test_a_stale_binding_is_recreated_without_old_access(hub, owner):
    """An identity still bound to an account that no longer exists (removed
    outside the Console) is re-created like an unavailable one: old grants go."""
    gs = store_for(hub)
    gs.upsert_identity(email="gone@example.com", owui_id="owui-gone")
    gs.grant("gone@example.com", "sales", "use_hub", actor="test")
    out = AccessService(hub).create_account(actor(), email="gone@example.com", name="Gone",
                                            grants=[])
    assert gs.identity("gone@example.com")["owui_id"] == out["owui_id"] != "owui-gone"
    assert not gs.can("gone@example.com", "sales", "use_hub")


def test_reset_password_returns_a_link_and_signs_them_out(hub, owner):
    service = AccessService(hub)
    service.create_account(actor(), email="ana@example.com", name="Ana", password="first secret",
                           grants=[])
    user = users.find_by_email(hub, "ana@example.com")
    sessions.create_session(hub, user, method="password")
    out = service.set_password(actor(), "ana@example.com")
    assert out["link"].startswith("/auth/set-password?token=")
    assert users.store(hub).password_hash(user["id"]) is None and live_sessions(hub, user["id"]) == 0
    assert service.set_password(actor(), "ana@example.com", "admin typed it") == {}
    rows = store_for(hub).read_access_audit(20, action="account_password_reset")
    assert len(rows) == 2 and "admin typed it" not in str(rows)


def test_google_only_accounts_have_no_password_to_reset(hub, owner, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    service = AccessService(hub)
    service.create_account(actor(), email="g@example.com", name="G", sign_in="google", grants=[])
    assert service.account_info(actor(), "g@example.com")["sign_in"] == "google"
    with pytest.raises(Denied) as exc:
        service.set_password(actor(), "g@example.com")
    assert exc.value.code == "google_managed"


def test_approve_role_and_delete(hub, owner):
    service = AccessService(hub)
    waiting = users.create(hub, email="w@example.com", name="W", status="pending",
                           password="their password", source="signup")
    users.sync_identity(hub, waiting)
    assert service.account_info(actor(), "w@example.com")["administrator"] == "pending"
    service.approve_account(actor(), "w@example.com")
    assert users.get(hub, waiting["id"])["status"] == "active"
    gs = store_for(hub)
    assert not gs.is_suspended("w@example.com") and not gs.identity("w@example.com")["pending"]
    sessions.create_session(hub, waiting, method="password")
    result = service.set_role(actor(), "w@example.com", "admin")
    assert result["administrator"] == "admin" and live_sessions(hub, waiting["id"]) == 0
    assert users.get(hub, waiting["id"])["role"] == "admin"
    assert gs.can("w@example.com", "*", "manage_access")
    service.set_role(actor(), "w@example.com", "user")
    assert users.get(hub, waiting["id"])["role"] == "user"
    sessions.create_session(hub, waiting, method="password")
    service.delete_account(actor(), "w@example.com")
    assert users.get(hub, waiting["id"]) is None and live_sessions(hub, waiting["id"]) == 0
    assert gs.is_suspended("w@example.com")  # unavailable until re-created


def test_the_last_administrator_is_kept(hub, owner):
    """Someone who administers only the Console can't demote or delete the
    last person who administers the web app too."""
    service = AccessService(hub)
    service.create_account(actor(), email="second@example.com", name="S", grants=[])
    store_for(hub).grant("second@example.com", "*", "manage_access", actor="test")
    second = Actor("second@example.com", "console", "session")
    with pytest.raises(Denied) as exc:
        service.set_role(second, OWNER, "user")
    assert exc.value.code == "last_admin"
    with pytest.raises(Denied) as exc:
        service.delete_account(second, OWNER)
    assert exc.value.code == "last_admin"
    assert users.find_by_email(hub, OWNER)["role"] == "admin"


def test_refresh_reconciles_from_hubzoid_accounts(hub, owner):
    gs = store_for(hub)
    gs.upsert_identity(email="vanished@example.com", owui_id="owui-vanished")
    count = AccessService(hub).refresh_accounts(actor())
    assert count == len(users.list_users(hub))
    assert gs.is_suspended("vanished@example.com")  # bound to no account any more
    assert not gs.is_suspended(OWNER)


def test_confirming_a_proposed_account_returns_a_link_that_is_not_stored(hub, owner):
    service = AccessService(hub)
    proposed = service.propose(Actor(OWNER, "web", "session"),
                               {"kind": "account", "hub": "sales", "email": "p@example.com",
                                "name": "P", "grant": ["use_hub"]})
    view = service.get_request(actor(), proposed["id"])
    assert view["sign_in_link"] is True
    out = service.confirm(actor(), proposed["id"], plan_hash=view["plan_hash"])
    assert out["status"] == "confirmed" and out["link"].startswith("/auth/set-password?token=")
    token = out["link"].split("token=", 1)[1]
    with store_for(hub).engine.connect() as conn:
        stored = conn.execute(text("SELECT result FROM hz_change_requests WHERE id=:i"),
                              {"i": proposed["id"]}).scalar()
    assert token not in stored and json.loads(stored)["subject"] == "p@example.com"


def test_admin_chat_tools_use_native_accounts_and_confirmation(hub, owner, monkeypatch):
    """Default-mode tools read Hubzoid accounts and only propose a new one."""
    monkeypatch.delenv("HUBZOID_MANAGEMENT_TOOLS", raising=False)
    gs = store_for(hub)
    gs.grant(OWNER, "sales", "access_tools", actor="test")
    tools = {tool.name: tool for tool in access_admin.make(SimpleNamespace(hub_dir=hub))}

    def invoke(name: str, args: dict) -> str:
        raw = json.dumps(args)
        tool = tools[name]
        ctx = ToolContext(context=None, tool_name=name, tool_call_id="test", tool_arguments=raw)
        return asyncio.run(tool.on_invoke_tool(ctx, raw))

    with identity_scope(Identity.make(OWNER, surface="web")):
        assert all(tool.is_enabled() for tool in tools.values())
        assert "sales" in invoke("my_management_scope", {})
        proposal = invoke("propose_new_account", {
            "email": "new-from-tool@example.com", "name": "New Person", "hub": "sales",
        })
        assert proposal.startswith("Proposed, not applied."), proposal
        assert users.find_by_email(hub, "new-from-tool@example.com") is None
        request_id = proposal.split("/confirm/", 1)[1].split()[0]
        view = AccessService(hub).get_request(actor(), request_id)
        out = AccessService(hub).confirm(actor(), request_id, plan_hash=view["plan_hash"])
        assert out["status"] == "confirmed" and out["link"].startswith("/auth/set-password?")
        assert users.find_by_email(hub, "new-from-tool@example.com") is not None
        listed = invoke("who_has_access", {"hub": "sales"})
        assert "New Person <new-from-tool@example.com>" in listed


# ---- the Console API on Hubzoid sessions ---------------------------------------------------

def console(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)
    app.include_router(portal.build_router(hub))
    return TestClient(app)


def test_console_signs_in_with_a_hubzoid_session(hub, owner):
    c = console(hub)
    assert c.get("/portal/api/me").status_code == 401
    c.post("/api/auth/login", json={"email": OWNER, "password": OWNER_PASSWORD}, headers=ORIGIN)
    me = c.get("/portal/api/me").json()
    assert me["subject"] == OWNER and me["org_admin"] is True
    assert me["accounts_configured"] is True and me["sign_in"]["links"] is True


def test_console_add_user_and_reset_password_return_links(hub, owner):
    c = console(hub)
    c.post("/api/auth/login", json={"email": OWNER, "password": OWNER_PASSWORD}, headers=ORIGIN)
    r = c.post("/portal/api/accounts", headers=ORIGIN,
               json={"email": "bo@example.com", "name": "Bo", "sign_in": "password", "grants": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["link"].startswith("/auth/set-password?token=") and body["expires_at"]
    r = c.post("/portal/api/accounts/bo@example.com/password", headers=ORIGIN, json={})
    assert r.status_code == 200 and r.json()["link"] != body["link"]
    assert c.get("/portal/api/accounts/bo@example.com").json()["sign_in"] == "password"
    # Cross-site writes are refused, as before.
    assert c.post("/portal/api/accounts/bo@example.com/password", json={}).status_code == 403


def test_console_in_local_mode_is_the_local_owner(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_AUTH")
    c = console(hub)
    me = c.get("/portal/api/me").json()
    assert me["subject"] == "admin@localhost" and me["org_admin"] is True


def test_deleting_an_account_drops_its_connection_tokens(hub, owner, monkeypatch):
    """Lane D's token store, when installed, forgets a deleted account's
    personal connection tokens; without it deletion works the same."""
    import sys
    import types

    service = AccessService(hub)
    service.create_account(actor(), email="gone@example.com", name="Gone", grants=[])
    user = users.find_by_email(hub, "gone@example.com")
    calls = []
    fake = types.ModuleType("hubzoid.connectors.tokens")
    fake.drop_user = lambda hub_dir, user_id: calls.append((str(hub_dir), user_id))
    monkeypatch.setitem(sys.modules, "hubzoid.connectors.tokens", fake)
    import hubzoid.connectors as connectors

    monkeypatch.setattr(connectors, "tokens", fake, raising=False)
    service.delete_account(actor(), "gone@example.com")
    assert calls == [(str(hub), user["id"])]
    monkeypatch.delitem(sys.modules, "hubzoid.connectors.tokens")
    monkeypatch.delattr(connectors, "tokens")
    service.create_account(actor(), email="gone2@example.com", name="Gone", grants=[])
    service.delete_account(actor(), "gone2@example.com")  # no token store: still deleted
    assert users.find_by_email(hub, "gone2@example.com") is None


def test_approval_clears_the_unavailable_marker(hub, owner):
    waiting = users.create(hub, email="w2@example.com", name="W", status="pending",
                           source="signup")
    users.sync_identity(hub, waiting)
    gs = store_for(hub)
    with gs.engine.connect() as conn:
        assert gs._meta_get(conn, "account_unavailable:w2@example.com") == "1"  # noqa: SLF001
    AccessService(hub).approve_account(actor(), "w2@example.com")
    with gs.engine.connect() as conn:
        assert gs._meta_get(conn, "account_unavailable:w2@example.com") == "0"  # noqa: SLF001


def test_localhost_addresses_get_no_account_or_link(hub, owner):
    service = AccessService(hub)
    with pytest.raises(Denied) as exc:
        service.create_account(actor(), email="x@app.localhost", name="X", grants=[])
    assert exc.value.code == "rejected"


def test_a_sign_in_link_opens_at_the_site_root():
    """In a gateway the hub's public URL ends in /b/<hub>; the sign-in pages
    are served at the site root, so the link drops that path."""
    from hubzoid.auth import links

    assert links.url("t1", "https://hub.example.org/b/finance") == "https://hub.example.org/auth/set-password?token=t1"
    assert links.url("t1", "http://127.0.0.1:3300/") == "http://127.0.0.1:3300/auth/set-password?token=t1"
    assert links.url("t1") == "/auth/set-password?token=t1"
