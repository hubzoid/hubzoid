"""Who is asking, by UI mode: ``access.session.verified_email`` and
``configured_owner`` (Hubzoid sessions by default, Open WebUI in legacy mode),
owner provisioning, the same-origin rule, and MCP OAuth consent on Hubzoid
sessions in sign-in and local mode."""
from __future__ import annotations

import asyncio
import re
from unittest import mock

import httpx
import pytest
from starlette.requests import Request

from hubzoid import deployment
from hubzoid.access import session, store_for
from hubzoid.auth import sessions, users

ENV = (
    "HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
    "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD",
    "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD", "HUBZOID_GATEWAY_ADMIN_EMAIL",
    "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_PORTAL_DEV", "HUBZOID_PORTAL_DEV_USER",
    "OWUI_INTERNAL_URL", "HUBZOID_OWUI_DB", "MCP_AUTH_MODE",
)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    sessions.reset_cache()
    yield d
    sessions.reset_cache()


def request(*, cookies: dict | None = None, headers: dict | None = None,
            host: str = "testserver") -> Request:
    raw = [(b"host", host.encode())]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw,
                    "query_string": b"", "scheme": "http", "server": (host, 80)})


def account(hub, email, role="user", **kw):
    user = users.create(hub, email=email, name=email.split("@")[0], role=role, **kw)
    return user, sessions.create_session(hub, user, method="password")


# ---- verified_email in the default mode ------------------------------------------------

def test_default_mode_reads_the_hubzoid_session(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    user, token = account(hub, "ana@example.com")
    assert session.verified_email(request(), hub) == ""
    assert session.verified_email(request(cookies={"token": "an-open-webui-jwt"}), hub) == ""
    assert session.verified_email(request(cookies={"hz_session": token}), hub) == "ana@example.com"
    identity = store_for(hub).identity("ana@example.com")
    assert identity["owui_id"] == user["id"] and not identity["pending"]
    assert session.verified_email(request(cookies={"hz_session": "forged"}), hub) == ""
    assert session.verified_email(request(cookies={"hz_session": token}), None) == ""


def test_bearer_session_tokens_only_where_allowed(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    _, token = account(hub, "ana@example.com")
    bearer = request(headers={"authorization": f"Bearer {token}"})
    assert session.verified_email(bearer, hub) == ""
    assert session.verified_email(bearer, hub, bearer=True) == "ana@example.com"


def test_only_the_configured_owner_is_provisioned(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "owner@example.com")
    gs = store_for(hub)
    _, other_admin = account(hub, "other@example.com", role="admin")
    assert session.verified_email(request(cookies={"hz_session": other_admin}), hub)
    assert not gs.can("other@example.com", "*", "manage_access")
    _, owner_user = account(hub, "owner@example.com", role="user")
    session.verified_email(request(cookies={"hz_session": owner_user}), hub)
    assert not gs.can("owner@example.com", "*", "manage_access")  # not an administrator
    owner = users.find_by_email(hub, "owner@example.com")
    users.set_role(hub, owner["id"], "admin")
    token = sessions.create_session(hub, owner, method="password")
    assert session.verified_email(request(cookies={"hz_session": token}), hub) == "owner@example.com"
    assert gs.can("owner@example.com", "*", "manage_access")
    assert gs.can("owner@example.com", "sales", "use_hub")


def test_local_mode_is_the_local_owner_and_provisions_it(hub):
    assert session.verified_email(request(), hub) == "admin@localhost"
    gs = store_for(hub)
    assert gs.can("admin@localhost", "*", "manage_access")
    assert session.verified_email(request(host="evil.example.com"), hub) == ""


def test_store_errors_are_a_503(hub, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    _, token = account(hub, "ana@example.com")
    monkeypatch.setattr(users, "on_sign_in", mock.Mock(side_effect=RuntimeError("store down")))
    with pytest.raises(HTTPException) as exc:
        session.verified_email(request(cookies={"hz_session": token}), hub)
    assert exc.value.status_code == 503


# ---- legacy mode keeps Open WebUI ---------------------------------------------------------

def test_legacy_mode_still_asks_open_webui(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setattr(deployment, "owui_url", lambda _: "http://127.0.0.1:43080")
    seen = {}

    def fake_get(url, headers=None, **kw):
        seen["url"], seen["auth"] = url, (headers or {}).get("Authorization")
        return httpx.Response(200, json={"role": "user", "email": "Bo@Example.com", "id": "owui-7"})

    monkeypatch.setattr(httpx, "get", fake_get)
    _, token = account(hub, "ana@example.com")
    # A Hubzoid session means nothing in legacy mode.
    assert session.verified_email(request(cookies={"hz_session": token}), hub) == ""
    assert session.verified_email(request(cookies={"token": "jwt"}), hub) == "bo@example.com"
    assert seen == {"url": "http://127.0.0.1:43080/api/v1/auths/", "auth": "Bearer jwt"}
    assert store_for(hub).identity("bo@example.com")["owui_id"] == "owui-7"


# ---- configured_owner --------------------------------------------------------------------

def test_configured_owner_in_default_mode(hub, monkeypatch, tmp_path):
    assert session.configured_owner(hub) == "admin@localhost"  # sign-in off
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "x@example.com")
    assert session.configured_owner(hub) == "admin@localhost"  # still off: the local owner
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    assert session.configured_owner(hub) == "x@example.com"
    monkeypatch.delenv("HUBZOID_ADMIN_EMAIL")
    monkeypatch.setenv("WEBUI_ADMIN_EMAIL", "Y@Example.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "z@example.com")
    assert session.configured_owner(hub) == "y@example.com"
    monkeypatch.delenv("WEBUI_ADMIN_EMAIL")
    assert session.configured_owner(hub) == "z@example.com"
    monkeypatch.delenv("HUBZOID_GATEWAY_ADMIN_EMAIL")
    deployment.save(tmp_path / "deployment.json",
                    hubs=[dict(key="sales", name="Sales", path=str(hub), model_id="sales")],
                    operational_url=f"sqlite:///{tmp_path / 'ops.db'}", owui_url="", owui_db="",
                    owner="Recorded@Example.com")
    assert session.configured_owner(hub) == "recorded@example.com"


def test_configured_owner_in_legacy_mode_is_unchanged(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "ignored@example.com")
    monkeypatch.setenv("HUBZOID_AUTH", "true")  # legacy reads WEBUI_AUTH only
    assert session.configured_owner(hub) == "admin@localhost"
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("WEBUI_ADMIN_EMAIL", "legacy@example.com")
    assert session.configured_owner(hub) == "legacy@example.com"


# ---- the same-origin rule -------------------------------------------------------------------

@pytest.mark.parametrize("origin,host,ok", [
    ("http://testserver", "testserver", True),
    ("https://hub.example.com", "127.0.0.1:3080", True),     # an allowed origin
    ("https://chat.example.org", "internal:8000", True),     # the second public name
    ("https://evil.example.com", "testserver", False),
    ("https://hub.example.com.evil.net", "testserver", False),
    ("null", "testserver", False),
    ("", "testserver", False),
])
def test_require_same_origin(monkeypatch, origin, host, ok):
    from fastapi import HTTPException

    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://chat.example.org")
    req = request(host=host, headers={"origin": origin} if origin else {})
    if ok:
        session.require_same_origin(req)
    else:
        with pytest.raises(HTTPException) as exc:
            session.require_same_origin(req)
        assert exc.value.status_code == 403


# ---- MCP OAuth consent on Hubzoid sessions ------------------------------------------------

def _mcp(hub_root, monkeypatch):
    from tests.test_mcp_oauth import RESOURCE
    from tests.test_mcp_server import _mk_hub

    hub = _mk_hub(hub_root, oauth_fixture=False)
    monkeypatch.setenv("MCP_SERVER", "true")
    monkeypatch.setenv("MCP_PUBLIC_URL", RESOURCE)
    return hub


def _run(hub, flow, cookies):
    from tests.test_mcp_oauth import app_for

    async def go():
        app = app_for(hub)
        async with app.lifespan(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://hub.example", cookies=cookies) as c:
                await flow(c)

    asyncio.run(go())


def test_mcp_consent_with_a_hubzoid_session(tmp_path, monkeypatch, hub):
    from tests.test_mcp_oauth import consent, exchange, register, rpc
    from tests.test_mcp_server import _result

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    mcp_hub = _mcp(tmp_path / "mcp", monkeypatch)
    user, token = account(mcp_hub, "alice@example.com")

    async def flow(c):
        client = await register(c)
        page = await c.get("/mcp/oauth/authorize", params={
            "client_id": client, "redirect_uri": "http://localhost:54321/callback",
            "response_type": "code", "scope": "hub:access", "state": "s",
            "code_challenge": "a" * 43, "code_challenge_method": "S256",
            "resource": "https://hub.example/mcp"})
        text = (await c.get(page.headers["location"])).text
        assert "alice@example.com" in text and "Open WebUI" not in text
        q = await consent(c, client)
        r = await exchange(c, client, q["code"][0])
        assert r.status_code == 200, r.text
        assert _result(await rpc(c, r.json()["access_token"]))["tools"]
        # Deleting the account ends the connection.
        users.delete(mcp_hub, user["id"])
        assert (await rpc(c, r.json()["access_token"])).status_code == 401

    _run(mcp_hub, flow, {"hz_session": token})


def test_mcp_consent_sends_signed_out_people_to_sign_in(tmp_path, monkeypatch, hub):
    from tests.test_mcp_oauth import CHALLENGE, REDIRECT, RESOURCE, register

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    mcp_hub = _mcp(tmp_path / "mcp", monkeypatch)

    async def flow(c):
        client = await register(c)
        r = await c.get("/mcp/oauth/authorize", params={
            "client_id": client, "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE})
        r = await c.get(r.headers["location"])
        assert r.status_code == 303
        assert re.match(r"^/auth\?redirect=%2Fmcp%2Foauth%2Fconsent%3Fticket%3D", r.headers["location"])

    _run(mcp_hub, flow, {})


def test_mcp_consent_in_local_mode(tmp_path, monkeypatch, hub):
    from tests.test_mcp_oauth import consent, exchange, register

    mcp_hub = _mcp(tmp_path / "mcp", monkeypatch)
    # Local mode serves the owner only on names it was told about.
    monkeypatch.setenv("HUBZOID_ALLOWED_ORIGINS", "https://hub.example")

    async def flow(c):
        client = await register(c)
        q = await consent(c, client)
        assert (await exchange(c, client, q["code"][0])).status_code == 200

    _run(mcp_hub, flow, {})


def test_mcp_account_dispatch(hub, monkeypatch, tmp_path):
    from hubzoid import mcp_oauth

    user = users.create(hub, email="m@example.com")
    users.sync_identity(hub, user)
    assert mcp_oauth.account(hub, email="m@example.com") == {"account_id": user["id"],
                                                               "email": "m@example.com"}
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(tmp_path / "missing.db"))
    assert mcp_oauth.account(hub, email="m@example.com") is None  # legacy reads Open WebUI
