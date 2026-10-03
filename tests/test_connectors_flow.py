"""Personal connections, end to end, against a REAL OAuth-protected MCP server.

The server is Hubzoid's own hosted MCP surface for a scratch hub (FastMCP with
``mcp_oauth.HubOAuth``) on a loopback port in 3500-3599: protected resource
metadata, authorization server metadata, dynamic client registration, PKCE,
RFC 8707 resource binding, rotating refresh tokens with replay detection, and
RFC 7009 revocation. Only its login step (the Open WebUI session behind its
consent page) is replaced; the consent form is driven like a browser would.

The app under test is the connection routes on a FastAPI app (sign-in off: every
request is the local owner, unless a test turns accounts on).
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from sqlalchemy import text

from hubzoid import connectors, secretbox
from hubzoid.access.identity import Identity
from hubzoid.connectors import per_user, registry, tokens
from tests import connectors_fakes as f

APP = "http://127.0.0.1:3599"  # the app's origin (in-process; nothing listens here)
SAME = {"origin": APP}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with f.OAuthMcp(tmp_path_factory.mktemp("oauth-mcp")) as srv:
        yield srv


@pytest.fixture
def browser(server):
    b = f.Browser(server)
    yield b
    b.close()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    f.clean_env(monkeypatch)
    return f.make_hub(tmp_path, "clienthub")


@pytest.fixture
def tc(hub):
    client = f.client_for(f.app_for(hub, journeys=True), APP)
    client.hub = hub
    return client


def add_connector(tc, server, **extra) -> str:
    r = tc.post("/portal/api/connectors", json={"name": "Team hub", "url": server.url, **extra},
                headers=SAME)
    assert r.status_code == 201, r.text
    cid = r.json()["connector"]["id"]
    f.grant_connector(tc.hub, cid)
    return cid


def start(tc, cid, **body) -> str:
    r = tc.post(f"/api/connections/{cid}/connect", json=body, headers=SAME)
    assert r.status_code == 200, r.text
    return r.json()["authorize_url"]


def connect(tc, browser, cid) -> httpx.Response:
    back = browser.consent(start(tc, cid))
    return tc.get(back)


def owner_id(hub) -> str:
    return tokens.user_id_for(hub, f.OWNER)


def expire_access_token(hub, cid):
    """Make the stored access token due for refresh, as time would."""
    uid = owner_id(hub)
    record = tokens.token_record(hub, uid, cid)
    record["expires_at"] = time.time() - 1
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_tokens SET token_enc = :t, expires_at = :e "
                          "WHERE user_id = :u AND connector_id = :c"),
                     {"t": secretbox.encrypt_json(hub, record), "e": record["expires_at"],
                      "u": uid, "c": cid})
    return record


async def call_knowledge(srv) -> str:
    """Call a tool through a per-turn descriptor with the runtime's own client."""
    from contextlib import AsyncExitStack

    from agents import RunConfig
    from agents.tool_context import ToolContext

    from hubzoid.runtime import open_personal_mcp

    async with AsyncExitStack() as stack:
        ((_server, tools),) = await open_personal_mcp(stack, [srv], set())
        tool = next(t for t in tools if t.name == "read_knowledge")
        ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c1",
                          tool_arguments='{"name": "widgets"}', run_config=RunConfig())
        out = await tool.on_invoke_tool(ctx, '{"name": "widgets"}')
        return out if isinstance(out, str) else str(out)


def servers_for_owner(hub):
    return per_user.per_user_servers(hub, Identity.make(f.OWNER, surface="web"))


# ---------------------------------------------------------------------------
# The whole journey
# ---------------------------------------------------------------------------
def test_discovery_registration_pkce_callback_tool_call(hub, tc, server, browser):
    cid = add_connector(tc, server)

    # The Console's test: discovery only, nothing registered.
    r = tc.post(f"/portal/api/connectors/{cid}/test", headers=SAME)
    result = r.json()
    assert r.status_code == 200 and result["ok"] is True
    assert result["registration"] == "dynamic" and result["requires_auth"] is True
    assert result["resource"] == server.url
    assert result["issuer"] == server.url + "/oauth"
    assert result["pkce"] == "S256"
    assert result["redirect_uri"] == f"{APP}/oauth/connectors/{cid}/callback"
    assert registry.get(hub, cid).registered is False

    # Start: dynamic registration, PKCE S256, resource, scope, state.
    url = start(tc, cid)
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(server.url + "/oauth/authorize?")
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43
    assert q["resource"] == [server.url]
    assert q["scope"] == ["hub:access"]
    assert q["redirect_uri"] == [f"{APP}/oauth/connectors/{cid}/callback"]
    state = q["state"][0]
    assert registry.get(hub, cid).registered is True
    with connectors.engine(hub).connect() as conn:
        rows = conn.execute(text("SELECT state, payload_enc FROM hz_connector_flows")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] != state and state not in str(rows)  # only its digest is stored
    payload = secretbox.decrypt_json(hub, rows[0][1])
    assert payload["code_verifier"] not in str(rows)

    # The person consents at the server; the browser comes back to our callback.
    back = browser.consent(url)
    assert back.startswith(f"/oauth/connectors/{cid}/callback?")
    # A different app instance finishes it: the flow lives in the shared store.
    other = f.client_for(f.app_for(hub), APP)
    r = other.get(back)
    assert r.status_code == 302
    assert r.headers["location"] == f"/account/connections?connected={cid}"
    assert r.headers["referrer-policy"] == "no-referrer"
    with connectors.engine(hub).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM hz_connector_flows")).scalar() == 0

    mine = tc.get("/api/connections").json()
    assert mine == [{"connector_id": cid, "name": "Team hub", "connected": True, "status": "ok",
                     "connected_at": mine[0]["connected_at"], "allowed": True,
                     "auth_type": "oauth", "enabled": True}]

    # Tokens at rest are encrypted; the API never shows them.
    record = tokens.token_record(hub, owner_id(hub), cid)
    assert record["access_token"].startswith("hz_at_") and record["refresh_token"]
    with connectors.engine(hub).connect() as conn:
        raw = str(conn.execute(text("SELECT * FROM hz_connector_tokens")).fetchall())
        raw += str(conn.execute(text("SELECT * FROM hz_connectors")).fetchall())
    for secret in (record["access_token"], record["refresh_token"], record["client"]["client_id"]):
        assert secret not in raw
    listing = tc.get("/portal/api/connectors").text
    assert record["access_token"] not in listing and record["client"]["client_id"] not in listing

    # The per-turn descriptor reaches the server as this person.
    (srv,) = servers_for_owner(hub)
    assert srv.key == f"my_{cid}" and srv.url == server.url and srv.server_id == cid
    assert srv.headers == {"Authorization": f"Bearer {record['access_token']}"}
    assert "42" in asyncio.run(call_knowledge(srv))


def test_refresh_rotation_replay_revocation_and_disconnect(hub, tc, server, browser):
    cid = add_connector(tc, server)
    assert connect(tc, browser, cid).status_code == 302
    before = tokens.token_record(hub, owner_id(hub), cid)

    # Due for refresh: a new access token and a rotated refresh token.
    expire_access_token(hub, cid)
    (srv,) = servers_for_owner(hub)
    after = tokens.token_record(hub, owner_id(hub), cid)
    assert after["access_token"] != before["access_token"]
    assert after["refresh_token"] != before["refresh_token"]
    assert srv.headers["Authorization"] == f"Bearer {after['access_token']}"
    assert after["expires_at"] > time.time() + 60
    assert "42" in asyncio.run(call_knowledge(srv))
    assert tokens.get(hub, owner_id(hub), cid).status == "ok"

    # Someone replays the spent refresh token: the server revokes the grant.
    r = httpx.post(server.url + "/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": before["refresh_token"],
        "client_id": before["client"]["client_id"], "resource": server.url})
    assert r.status_code in (400, 401) and r.json()["error"] == "invalid_grant"
    # Hubzoid's next refresh is refused: the connection is expired, not retried.
    expire_access_token(hub, cid)
    assert servers_for_owner(hub) == []
    conn = tokens.get(hub, owner_id(hub), cid)
    assert conn.status == "expired" and conn.error == "refresh_rejected:invalid_grant"
    assert tc.get("/api/connections").json()[0]["status"] == "expired"

    # Reconnecting replaces it.
    assert connect(tc, browser, cid).status_code == 302
    assert tokens.get(hub, owner_id(hub), cid).status == "ok"
    live = tokens.token_record(hub, owner_id(hub), cid)["access_token"]
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    hdrs = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {live}"}
    assert httpx.post(server.url, json=rpc, headers=hdrs).status_code == 200

    # Disconnect: revoked at the server (RFC 7009), then deleted here.
    r = tc.delete(f"/api/connections/{cid}", headers=SAME)
    assert r.status_code == 204
    assert httpx.post(server.url, json=rpc, headers=hdrs).status_code == 401
    assert tokens.get(hub, owner_id(hub), cid) is None
    assert servers_for_owner(hub) == []
    assert tc.get("/api/connections").json()[0]["connected"] is False


def test_pre_registered_client_is_used_without_registration(hub, tc, server, browser):
    client_id = server.register_client(f"{APP}/oauth/connectors/team_hub/callback")
    cid = add_connector(tc, server, id="team_hub", client_id=client_id)
    result = tc.post(f"/portal/api/connectors/{cid}/test", headers=SAME).json()
    assert result["ok"] is True and result["registration"] == "pre-registered"
    url = start(tc, cid)
    assert parse_qs(urlsplit(url).query)["client_id"] == [client_id]
    r = tc.get(browser.consent(url))
    assert r.headers["location"] == f"/account/connections?connected={cid}"
    assert registry.get(hub, cid).registered is False  # nothing was registered
    record = tokens.token_record(hub, owner_id(hub), cid)
    assert record["client"] == {"client_id": client_id, "client_secret": None,
                                "auth_method": "none", "source": "pre-registered"}
    assert "42" in asyncio.run(call_knowledge(servers_for_owner(hub)[0]))


# ---------------------------------------------------------------------------
# Refusals at the callback
# ---------------------------------------------------------------------------
def test_a_callback_is_single_use(hub, tc, server, browser):
    cid = add_connector(tc, server)
    back = browser.consent(start(tc, cid))
    assert tc.get(back).headers["location"] == f"/account/connections?connected={cid}"
    first = tokens.get(hub, owner_id(hub), cid)
    r = tc.get(back)
    assert r.status_code == 302
    assert r.headers["location"] == f"/account/connections?connector={cid}&error=invalid_state"
    assert tokens.get(hub, owner_id(hub), cid).version == first.version


def test_an_expired_authorization_is_refused(hub, tc, server, browser):
    cid = add_connector(tc, server)
    url = start(tc, cid)
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_flows SET expires_at = :t"), {"t": time.time() - 1})
    r = tc.get(browser.consent(url))
    assert r.headers["location"].endswith("error=expired")
    assert tokens.get(hub, owner_id(hub), cid) is None


def test_unknown_or_missing_state_is_refused(hub, tc, server):
    cid = add_connector(tc, server)
    for query in ("code=abc", "code=abc&state=forged", "error=access_denied"):
        r = tc.get(f"/oauth/connectors/{cid}/callback?{query}")
        assert r.headers["location"].endswith("error=invalid_state"), query


def test_iss_that_does_not_match_the_issuer_is_refused(hub, tc, server, browser):
    cid = add_connector(tc, server)
    back = browser.consent(start(tc, cid))
    r = tc.get(back + "&iss=https%3A%2F%2Fevil.example")
    assert r.headers["location"].endswith("error=issuer_mismatch")
    assert tokens.get(hub, owner_id(hub), cid) is None
    # The right issuer is accepted.
    back = browser.consent(start(tc, cid))
    r = tc.get(back + "&" + urlencode({"iss": server.url + "/oauth"}))
    assert r.headers["location"] == f"/account/connections?connected={cid}"


def test_iss_is_required_when_the_server_advertises_it(hub, tc, server, browser, monkeypatch):
    from hubzoid.connectors import discovery, oauth_flow

    real = discovery.discover

    def advertising(url, **kw):
        found = real(url, **kw)
        found.iss_supported = True
        return found

    monkeypatch.setattr(oauth_flow, "discover", advertising)
    cid = add_connector(tc, server)
    r = tc.get(browser.consent(start(tc, cid)))
    assert r.headers["location"].endswith("error=issuer_mismatch")
    assert tokens.get(hub, owner_id(hub), cid) is None
    back = browser.consent(start(tc, cid))
    r = tc.get(back + "&" + urlencode({"iss": server.url + "/oauth"}))
    assert r.headers["location"] == f"/account/connections?connected={cid}"


def test_denied_consent_says_so(hub, tc, server, browser):
    cid = add_connector(tc, server)
    back = browser.consent(start(tc, cid, return_to="/account/connections?from=chat"),
                           decision="deny")
    r = tc.get(back)
    assert r.headers["location"] == "/account/connections?from=chat&error=access_denied"
    assert tokens.get(hub, owner_id(hub), cid) is None


def test_another_account_cannot_finish_someone_elses_authorization(hub, tc, server, browser,
                                                                   monkeypatch):
    alice, bob = "alice@example.org", "bob@example.org"
    f.accounts(monkeypatch, hub, {alice: ("u-alice", "admin"), bob: ("u-bob", "user")})
    from hubzoid.access import store_for

    store_for(hub).bootstrap([alice])
    r = tc.post("/portal/api/connectors", json={"name": "Team hub", "url": server.url},
                headers={**SAME, "x-test-user": alice})
    assert r.status_code == 201, r.text
    cid = r.json()["connector"]["id"]
    f.grant_connector(hub, cid, alice, bob)
    r = tc.post(f"/api/connections/{cid}/connect",
                json={"return_to": "/account/connections?mine=1"},
                headers={**SAME, "x-test-user": alice})
    back = browser.consent(r.json()["authorize_url"])

    # Bob arrives with Alice's callback: refused, sent to his own page, not hers.
    r = tc.get(back, headers={"x-test-user": bob})
    assert r.headers["location"] == f"/account/connections?connector={cid}&error=wrong_user"
    # The authorization is spent: Alice's own callback cannot use it either.
    r = tc.get(back, headers={"x-test-user": alice})
    assert r.headers["location"].endswith("error=invalid_state")
    assert tokens.for_user(hub, "u-alice") == [] and tokens.for_user(hub, "u-bob") == []
    with connectors.engine(hub).connect() as conn:
        rows = conn.execute(text("SELECT subject, decision, reason FROM hz_access_decisions "
                                 "WHERE tool = :t"), {"t": f"connect:{cid}"}).fetchall()
    assert (bob, "deny", "wrong_user") in [tuple(r) for r in rows]

    # Signed out at the callback: nothing is consumed, the person signs in again.
    r = tc.post(f"/api/connections/{cid}/connect", json={}, headers={**SAME, "x-test-user": alice})
    back = browser.consent(r.json()["authorize_url"])
    r = tc.get(back)
    assert r.headers["location"] == f"/account/connections?connector={cid}&error=unauthenticated"
    r = tc.get(back, headers={"x-test-user": alice})
    assert r.headers["location"] == f"/account/connections?connected={cid}"
    assert [c.connector_id for c in tokens.for_user(hub, "u-alice")] == [cid]


def test_a_connector_changed_mid_flow_is_refused(hub, tc, server, browser):
    cid = add_connector(tc, server)
    url = start(tc, cid)
    r = tc.patch(f"/portal/api/connectors/{cid}", json={"enabled": False}, headers=SAME)
    assert r.status_code == 200
    r = tc.get(browser.consent(url))
    assert r.headers["location"].endswith("error=connector_changed")
    assert tokens.get(hub, owner_id(hub), cid) is None


# ---------------------------------------------------------------------------
# The connection journey (a link from WhatsApp or Telegram) in the default mode
# ---------------------------------------------------------------------------
def _journey(hub, cid):
    """The connect_account tool's call, as the owner writing from WhatsApp.
    This hub's access is not managed in the Console, so the journey needs the
    legacy group named after the capability."""
    from hubzoid import _request_ctx, connect_journey
    from hubzoid.access import identity_scope

    who = Identity.make(f.OWNER, groups=[f"connector_{cid}"], surface="whatsapp")
    with identity_scope(who), _request_ctx.chat_scope("whatsapp-919800000001"):
        return connect_journey.start(hub, app=cid)


def test_a_journey_link_connects_through_hubzoid(hub, tc, server, browser, monkeypatch):
    from hubzoid.connect_journey import store

    monkeypatch.setenv("HUBZOID_CONNECT_JOURNEY", "true")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "web,api,mcp,whatsapp")
    cid = add_connector(tc, server)
    link = _journey(hub, cid)
    assert link["state"] == "link" and link["url"].endswith(f"/portal/connect/{link['id']}")
    jid = link["id"]
    j = store.get(hub, jid)
    assert j["provider"] == "hz_connector" and j["provider_ref"] == cid

    page = tc.get(f"/portal/connect/{jid}")  # sign-in off: the local owner
    assert page.status_code == 200 and "Continue" in page.text
    r = tc.post(f"/portal/connect/{jid}/start", headers=SAME)
    assert r.status_code == 303
    assert r.headers["location"].startswith(server.url + "/oauth/authorize?")
    assert "hz_connect" not in r.headers.get("set-cookie", "")
    back = browser.consent(r.headers["location"])
    r = tc.get(back)
    assert r.headers["location"] == f"/portal/connect/{jid}/done"
    done = tc.get(f"/portal/connect/{jid}/done")
    assert done.status_code == 200 and "is connected" in done.text
    assert store.get(hub, jid)["status"] == "connected"
    # Asking again says it is connected, by the provider's own records.
    assert _journey(hub, cid)["state"] == "connected"


def test_a_refused_journey_ends_failed_at_once(hub, tc, server, browser, monkeypatch):
    from hubzoid.connect_journey import store

    monkeypatch.setenv("HUBZOID_CONNECT_JOURNEY", "true")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "web,api,mcp,whatsapp")
    cid = add_connector(tc, server)
    jid = _journey(hub, cid)["id"]
    r = tc.post(f"/portal/connect/{jid}/start", headers=SAME)
    r = tc.get(browser.consent(r.headers["location"], decision="deny"))
    assert r.headers["location"] == f"/portal/connect/{jid}/done?error=access_denied"
    assert store.get(hub, jid)["status"] == "failed"
    done = tc.get(f"/portal/connect/{jid}/done")
    assert "was not connected" in done.text


_CONSUMER = """
import sys, time
from pathlib import Path
from hubzoid.connectors import oauth_flow
hub, state, me, gate = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
oauth_flow._consume(hub, "warm-up-" + me.name)  # imports and opens the database first
me.touch()
while not gate.exists():
    time.sleep(0.005)
try:
    print("WON" if oauth_flow._consume(hub, state) else "LOST")
except Exception as exc:
    print("ERROR", type(exc).__name__, exc)
"""


def test_authorizations_are_consumed_once_by_concurrent_processes_on_sqlite(hub, tmp_path):
    """Several bridges share one SQLite file: concurrent callbacks never fail
    with a lock error, and a state is taken exactly once."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from hubzoid.connectors import oauth_flow

    states = [f"distinct-state-{i}" for i in range(4)] + ["shared-state"] * 4
    with connectors.engine(hub).begin() as conn:
        for state in sorted(set(states)):
            conn.execute(text(
                "INSERT INTO hz_connector_flows (state, user_id, connector_id, payload_enc, "
                "return_to, created_at, expires_at) VALUES (:s, 'u', 'c', 'x', NULL, 0, :e)"),
                {"s": oauth_flow._digest(state), "e": time.time() + 600})
    gate = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    procs = [subprocess.Popen([sys.executable, "-c", _CONSUMER, str(hub), state,
                               str(tmp_path / f"ready-{i}"), str(gate)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for i, state in enumerate(states)]
    deadline = time.time() + 120
    while not all((tmp_path / f"ready-{i}").exists() for i in range(len(states))):
        assert time.time() < deadline and all(p.poll() is None for p in procs), \
            [p.communicate()[1][-400:] for p in procs if p.poll() is not None]
        time.sleep(0.05)
    gate.touch()
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert not [o for o in outs if not o.startswith(("WON", "LOST"))], outs
    assert outs[:4] == ["WON"] * 4
    assert sorted(outs[4:]) == ["LOST", "LOST", "LOST", "WON"]
