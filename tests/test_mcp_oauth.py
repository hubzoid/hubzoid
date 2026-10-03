"""Real OAuth HTTP flows; only the external OWUI login service is replaced."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import text

from hubzoid import mcp_server, settings as settingslib
from tests.test_mcp_server import _mk_hub, _mk_owui_db, _rpc, _result

RESOURCE = "https://hub.example/mcp"
VERIFIER = "a" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
REDIRECT = "http://localhost:54321/callback"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path, oauth_fixture=False)
    db = tmp_path / "owui.db"
    _mk_owui_db(db)
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    # Open WebUI accounts: the legacy UI mode (test_auth_session_dispatch
    # covers MCP consent on Hubzoid accounts).
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setenv("MCP_SERVER", "true")
    monkeypatch.delenv("MCP_AUTH_MODE", raising=False)
    monkeypatch.setenv("MCP_PUBLIC_URL", RESOURCE)
    from hubzoid.access import session

    monkeypatch.setattr(
        session,
        "verified_email",
        lambda req, hub_dir: (
            "alice@example.com" if req.cookies.get("token") == "owui-session" else ""
        ),
    )
    return hub, db


def app_for(hub):
    return mcp_server.build_mcp_app(hub, settings=settingslib.load(hub))


async def register(c, redirect=REDIRECT, *, name="Claude Test"):
    r = await c.post(
        "/mcp/oauth/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "hub:access",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def consent(c, client, *, decision="allow", resource=RESOURCE):
    r = await c.get(
        "/mcp/oauth/authorize",
        params={
            "client_id": client,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "hub:access",
            "state": "original-state",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": resource,
        },
    )
    assert r.status_code == 302, r.text
    url = r.headers["location"]
    r = await c.get(url)
    assert r.status_code == 200, r.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    r = await c.post(
        url,
        data={"csrf": csrf, "decision": decision},
        headers={"Origin": "https://hub.example"},
    )
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query)


async def exchange(c, client, code, **kwargs):
    return await c.post(
        "/mcp/oauth/token",
        data={
            "client_id": client,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **kwargs,
        },
    )


async def rpc(c, token):
    return await c.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
    )


def run_flow(hub, flow):
    async def run():
        app = app_for(hub)
        async with app.lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://hub.example",
                cookies={"token": "owui-session"},
            ) as c:
                await flow(c)

    asyncio.run(run())


def test_oauth_lifecycle_rejects_old_api_keys(setup):
    hub, _ = setup

    async def flow(c):
        r = await c.post("/mcp", json=_rpc("tools/list"))
        assert r.status_code == 401
        assert "oauth-protected-resource" in r.headers["www-authenticate"]
        assert (await c.get("/.well-known/oauth-protected-resource/mcp")).json()[
            "resource"
        ] == RESOURCE
        md = (await c.get("/.well-known/oauth-authorization-server/mcp/oauth")).json()
        assert md["authorization_endpoint"] == RESOURCE + "/oauth/authorize"
        assert md["token_endpoint_auth_methods_supported"] == ["none"]
        client = await register(c)
        q = await consent(c, client)
        assert q["state"] == ["original-state"]
        code = q["code"][0]
        assert (
            await exchange(c, client, code, code_verifier="wrong")
        ).status_code >= 400
        assert (
            await exchange(c, client, code, resource="https://evil.example/mcp")
        ).status_code >= 400
        r = await exchange(c, client, code)
        assert r.status_code == 200, r.text
        tokens = r.json()
        assert (await exchange(c, client, code)).status_code >= 400
        assert _result(await rpc(c, tokens["access_token"]))["tools"]
        assert (await rpc(c, "sk-test")).status_code == 401
        r = await c.post(
            "/mcp/oauth/token",
            data={
                "client_id": client,
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            },
        )
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["refresh_token"] != tokens["refresh_token"]
        # A stolen old refresh token being replayed revokes the entire grant.
        r = await c.post(
            "/mcp/oauth/token",
            data={
                "client_id": client,
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            },
        )
        assert r.status_code >= 400
        assert (await rpc(c, new["access_token"])).status_code == 401

    run_flow(hub, flow)


def test_login_csrf_deny_and_unsafe_redirect(setup):
    hub, _ = setup

    async def flow(c):
        client = await register(c)
        c.cookies.clear()
        r = await c.get(
            "/mcp/oauth/authorize",
            params={
                "client_id": client,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "resource": RESOURCE,
            },
        )
        r = await c.get(r.headers["location"])
        assert r.status_code == 303 and r.headers["location"].startswith(
            "/auth?redirect="
        )
        c.cookies.set("token", "owui-session")
        q = await consent(c, client, decision="deny")
        assert q["error"] == ["access_denied"] and "code" not in q
        r = await c.post(
            "/mcp/oauth/register",
            json={
                "redirect_uris": ["https://evil.example/cb#fragment"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert r.status_code >= 400

    run_flow(hub, flow)


def test_consent_escapes_names_and_preserves_denied_access(setup):
    hub, _ = setup
    (hub / "AGENTS.md").write_text("---\nname: Finance & Payroll\n---\nHelp.\n")

    async def flow(c):
        client = await register(c, name='<script>alert("client")</script>')
        r = await c.get("/mcp/oauth/authorize", params={
            "client_id": client, "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE,
        })
        url = r.headers["location"]
        r = await c.get(url)
        assert r.status_code == 200
        assert "Finance &amp; Payroll" in r.text and "&lt;script&gt;" in r.text
        assert "<script>" not in r.text
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert r.headers["cache-control"] == "no-store"
        from hubzoid.access import store_for

        store_for(hub).revoke("alice@example.com", hub.name, "use_hub", actor="test")
        r = await c.get(url)
        assert r.status_code == 403
        assert r.headers["content-type"].startswith("text/html")
        assert "Hub access needed" in r.text and 'value="allow"' not in r.text
        assert 'href="/"' in r.text

    run_flow(hub, flow)


def test_persistence_revocation_and_account_deletion(setup):
    hub, db = setup
    state = {}

    async def issue(c):
        state["client"] = await register(c)
        q = await consent(c, state["client"])
        state.update((await exchange(c, state["client"], q["code"][0])).json())

    run_flow(hub, issue)

    async def after_restart(c):
        assert (await rpc(c, state["access_token"])).status_code == 200
        r = await c.get("/mcp/oauth/connections")
        assert "Claude Test" in r.text
        import sqlite3

        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM user")
        assert (await rpc(c, state["access_token"])).status_code == 401

    run_flow(hub, after_restart)


def test_oauth_configuration_and_mode(setup, monkeypatch):
    hub, _ = setup
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://public.example/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        app_for(hub)
    monkeypatch.setenv("MCP_PUBLIC_URL", RESOURCE)

    async def flow(c):
        assert (await rpc(c, "sk-test")).status_code == 401

    run_flow(hub, flow)


def test_revoke_csrf_and_wrong_client(setup):
    hub, _ = setup

    async def flow(c):
        client = await register(c)
        other = await register(c)
        q = await consent(c, client)
        code = q["code"][0]
        assert (await exchange(c, other, code)).status_code >= 400
        tokens = (await exchange(c, client, code)).json()
        page = await c.get("/mcp/oauth/connections")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        grant = re.search(r'name="grant" value="([^"]+)"', page.text).group(1)
        for origin, proof in [
            ("https://evil.example", csrf),
            ("https://hub.example", "bad"),
        ]:
            r = await c.post(
                "/mcp/oauth/connections",
                data={"grant": grant, "csrf": proof},
                headers={"Origin": origin},
            )
            assert r.status_code == 403
            assert (await rpc(c, tokens["access_token"])).status_code == 200
        r = await c.post(
            "/mcp/oauth/connections",
            data={"grant": grant, "csrf": csrf},
            headers={"Origin": "https://hub.example"},
        )
        assert r.status_code == 303
        assert (await rpc(c, tokens["access_token"])).status_code == 401

    run_flow(hub, flow)


def test_gateway_discovery_routes_and_per_hub_settings(tmp_path, monkeypatch):
    from hubzoid import gateway

    for name, port in [("a", 8001), ("b", 8002)]:
        h = tmp_path / name
        h.mkdir()
        (h / "AGENTS.md").write_text(f"---\nname: {name}\n---\n")
        extra = (
            f"MCP_PUBLIC_URL=https://hub.example/b/{name}/mcp\n" if name == "a" else ""
        )
        (h / ".env").write_text(
            f"BRIDGE_PORT={port}\nMCP_SERVER={str(name == 'a').lower()}\nBRIDGE_API_KEYS=test-key\n"
            + extra
        )
    gp = gateway.plan([tmp_path / "a", tmp_path / "b"])
    assert gp.backends[0].mcp
    assert not gp.backends[1].mcp
    assert gp.backends[1].mcp_public_url == ""
    routes = gp.edge_routes()
    assert any(
        r["prefix"] == "/.well-known/oauth-protected-resource/b/a/mcp" for r in routes
    )
    assert any(
        r["prefix"] == "/.well-known/oauth-authorization-server/b/a/mcp/oauth"
        for r in routes
    )
    assert not any(
        "/.well-known/" in r["prefix"] and "/b/b/" in r["prefix"] for r in routes
    )


def test_gateway_metadata_and_cross_resource_token(setup, monkeypatch):
    hub, _ = setup
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://hub.example/b/team/mcp")

    async def flow(c):
        md = await c.get("/.well-known/oauth-protected-resource/b/team/mcp")
        assert md.status_code == 200
        assert md.json()["resource"] == "https://hub.example/b/team/mcp"
        md = await c.get("/.well-known/oauth-authorization-server/b/team/mcp/oauth")
        assert md.status_code == 200
        assert (
            md.json()["token_endpoint"] == "https://hub.example/b/team/mcp/oauth/token"
        )

    run_flow(hub, flow)


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM user",
        "UPDATE user SET id='replacement'",
        "UPDATE user SET email='someone-else@example.com'",
    ],
)
def test_account_changes_invalidate_grant(setup, change):
    hub, db = setup

    async def flow(c):
        import sqlite3

        client = await register(c)
        q = await consent(c, client)
        token = (await exchange(c, client, q["code"][0])).json()["access_token"]
        assert (await rpc(c, token)).status_code == 200
        with sqlite3.connect(db) as con:
            con.execute(change)
        assert (await rpc(c, token)).status_code == 401

    run_flow(hub, flow)


def test_code_concurrent_exchange_and_hashed_storage(setup):
    hub, _ = setup

    async def flow(c):
        client = await register(c)
        q = await consent(c, client)
        code = q["code"][0]
        results = await asyncio.gather(
            exchange(c, client, code), exchange(c, client, code)
        )
        assert sorted(r.status_code == 200 for r in results) == [False, True]
        tokens = next(r.json() for r in results if r.status_code == 200)
        from hubzoid.mcp_oauth_store import OAuthStore

        store = OAuthStore(hub, RESOURCE)
        with store.engine.connect() as con:
            rows = con.execute(
                text("SELECT digest,payload FROM hz_mcp_oauth")
            ).fetchall()
        stored = str(rows)
        for secret in [
            code,
            tokens["access_token"],
            tokens["refresh_token"],
            "owui-session",
        ]:
            assert secret not in stored

    run_flow(hub, flow)


def test_expired_access_and_grant(setup):
    hub, _ = setup

    async def flow(c):
        from hubzoid.mcp_oauth_store import OAuthStore

        client = await register(c)
        q = await consent(c, client)
        tokens = (await exchange(c, client, q["code"][0])).json()
        store = OAuthStore(hub, RESOURCE)
        with store.engine.begin() as con:
            con.execute(text("UPDATE hz_mcp_oauth SET expires=0 WHERE kind='access'"))
        assert (await rpc(c, tokens["access_token"])).status_code == 401
        with store.engine.begin() as con:
            con.execute(text("UPDATE hz_mcp_oauth SET expires=0 WHERE kind='grant'"))
        r = await c.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client,
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            },
        )
        assert r.status_code >= 400

    run_flow(hub, flow)


def test_public_url_is_canonical(setup, monkeypatch):
    hub, _ = setup
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://HUB.example:443/mcp")

    async def flow(c):
        client = await register(c)
        q = await consent(c, client)
        assert (await exchange(c, client, q["code"][0])).status_code == 200

    run_flow(hub, flow)


def test_gateway_wrong_public_path_is_rejected(tmp_path):
    from hubzoid import gateway

    h = tmp_path / "sales"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: sales\n---\n")
    (h / ".env").write_text(
        "MCP_SERVER=true\nMCP_PUBLIC_URL=https://hub.example/mcp\nBRIDGE_API_KEYS=test\n"
    )
    with pytest.raises(ValueError, match="MCP_PUBLIC_URL"):
        gateway.plan([h])


@pytest.mark.parametrize("obsolete_mode", ["legacy", "dual"])
def test_obsolete_mode_cannot_restore_api_keys(setup, monkeypatch, obsolete_mode):
    hub, _ = setup
    monkeypatch.setenv("MCP_AUTH_MODE", obsolete_mode)

    async def flow(c):
        assert (await rpc(c, "sk-test")).status_code == 401
        assert (
            await c.get("/.well-known/oauth-protected-resource/mcp")
        ).status_code == 200

    run_flow(hub, flow)
