"""The connection journey without a browser: start checks, state machine,
provider verification (Open WebUI and Composio), duplicate-provider refusal,
the continuation store and the connect_account tool.

Open WebUI verification runs against a seeded, Open WebUI-shaped SQLite
database. Composio runs against a fake client with the SDK's shapes.
"""
from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from hubzoid import _request_ctx, connect_journey, connections
from hubzoid.access import Identity, identity_scope
from hubzoid.connect_journey import providers, store
from tests import connect_helpers as h

ALICE, BOB = "alice@example.org", "bob@example.org"
SECRET = "journey-secret"
CHAT = "whatsapp-919800000001"


class _Hub(type(Path())):
    """A hub path that can also carry the test's fakes."""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = _Hub(tmp_path / "mailhub")
    hub.mkdir()
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ua", ALICE), ("ub", BOB)], secret=SECRET, servers=[
        {"id": "gmail", "name": "Gmail", "url": "https://gmail-mcp.example.org/mcp"},
        {"id": "wiki", "name": "Wiki", "url": "https://wiki.example.org/mcp", "auth_type": "bearer"},
    ])
    h.owui_env(monkeypatch, db, SECRET)
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_CONNECT_JOURNEY", "true")
    monkeypatch.setenv("WEBUI_URL", "https://hub.example.org")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "owui,web,api,mcp,whatsapp")
    monkeypatch.delenv("HUBZOID_CONNECT_TTL", raising=False)
    connections.set_gate(connections.Connections(client=None, allowed=()))
    hub.db = db  # type: ignore[attr-defined]
    return hub


def _as(email, surface="whatsapp", groups=("connector_gmail",), chat=CHAT):
    class _Scope:
        def __enter__(self):
            self._a = identity_scope(Identity.make(user=email, groups=list(groups), surface=surface))
            self._b = _request_ctx.chat_scope(chat)
            self._a.__enter__()
            self._b.__enter__()
            return self

        def __exit__(self, *exc):
            self._b.__exit__(*exc)
            self._a.__exit__(*exc)

    return _Scope()


def _audit(hub):
    from sqlalchemy import text

    import hubzoid.db as db
    with db.operational_engine(hub).connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT subject, tool, decision, reason FROM hz_access_decisions ORDER BY ts"))]


# ---------------------------------------------------------------------------
# start(): checks, then a bound link
# ---------------------------------------------------------------------------
def test_start_returns_a_bound_link_and_never_a_provider_url(hub):
    with _as(ALICE):
        r = connect_journey.start(hub, app="Gmail")
    assert r["state"] == "link" and r["subject"] == ALICE
    assert r["url"] == f"https://hub.example.org/portal/connect/{r['id']}"
    assert store.ID_RE.match(r["id"]) and len(r["id"]) >= 32
    assert "oauth" not in r["url"] and "gmail-mcp" not in r["url"]
    j = store.get(hub, r["id"])
    assert (j["subject"], j["surface"], j["chat_id"], j["handle"]) == (ALICE, "whatsapp", CHAT, "919800000001")
    assert (j["app"], j["provider"], j["provider_ref"], j["status"]) == ("gmail", "owui_mcp", "gmail", "pending")
    assert 590 <= j["expires"] - j["created"] <= 600
    assert (ALICE, "connect:gmail", "allow", "link") in _audit(hub)


def test_ids_are_unguessable_and_malformed_ids_are_never_looked_up(hub):
    ids = {store.new_id() for _ in range(200)}
    assert len(ids) == 200 and all(store.ID_RE.match(i) for i in ids)
    assert store.get(hub, "../../etc") is None and store.get(hub, "short") is None


def test_legacy_hub_needs_the_legacy_group_of_the_same_name(hub):
    with _as(ALICE, groups=()):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "denied" and "connector_gmail" in err.value.message
    assert (ALICE, "connect:gmail", "deny", "no-group") in _audit(hub)


def test_managed_hub_needs_the_console_grant(hub):
    import hubzoid.access as access
    gs = access.store_for(hub)
    gs.set_authoritative(True, hub=hub.name)
    with _as(ALICE):  # a legacy group is no longer enough
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "denied"
    gs.grant(ALICE, hub.name, "connector_gmail")
    with _as(ALICE, groups=()):
        assert connect_journey.start(hub, app="gmail")["state"] == "link"


@pytest.mark.parametrize("surface", ["slack-channel", "slack", "telegram"])
def test_surfaces_outside_the_restricted_list_are_refused(hub, surface):
    with _as(ALICE, surface=surface):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "denied" and "channel" in err.value.message


def test_whatsapp_must_be_listed_as_a_restricted_surface(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "owui,web,api,mcp")
    with _as(ALICE):
        with pytest.raises(connect_journey.JourneyError):
            connect_journey.start(hub, app="gmail")


def test_anonymous_and_unknown_apps(hub):
    with pytest.raises(connect_journey.JourneyError) as err:
        connect_journey.start(hub, app="gmail")
    assert err.value.code == "anonymous"
    with _as(ALICE, groups=("connector_notion",)):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="notion")
    assert err.value.code == "unavailable"
    with _as(ALICE, groups=("connector_wiki",)):  # not an OAuth server: nothing to connect
        with pytest.raises(connect_journey.JourneyError):
            connect_journey.start(hub, app="wiki")


def test_already_connected_says_so_and_reconnect_makes_a_new_link(hub):
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-a")
    with _as(ALICE):
        assert connect_journey.start(hub, app="gmail") == {
            "state": "connected", "app": "gmail", "label": "Gmail"}
        r = connect_journey.start(hub, app="gmail", reconnect=True)
    assert r["state"] == "link"


def test_a_newer_link_supersedes_the_older_one(hub):
    with _as(ALICE):
        first = connect_journey.start(hub, app="gmail")
        second = connect_journey.start(hub, app="gmail")
    assert store.get(hub, first["id"])["status"] == "superseded"
    assert store.get(hub, second["id"])["status"] == "pending"
    assert store.get(hub, first["id"])["notified"] is not None  # superseded ends silently


def test_one_person_cannot_supersede_another(hub):
    with _as(ALICE):
        a = connect_journey.start(hub, app="gmail")
    with _as(BOB, chat="whatsapp-2"):
        connect_journey.start(hub, app="gmail")
    assert store.get(hub, a["id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# Open WebUI verification (never from callback parameters)
# ---------------------------------------------------------------------------
def _started(hub, email=ALICE, reconnect=False):
    with _as(email):
        r = connect_journey.start(hub, app="gmail", reconnect=reconnect)
    assert store.mark_started(hub, r["id"], ttl=600)
    return store.get(hub, r["id"])


def test_owui_verify_needs_a_session_created_after_start_with_a_usable_token(hub):
    j = _started(hub)
    prov = providers.OwuiMcpProvider(hub, "gmail")
    assert prov.verify(j) == "pending"
    # Bob connecting does not connect Alice.
    h.connect(hub.db, user_id="ub", server_id="gmail", secret=SECRET, access_token="AT-b")
    assert prov.verify(j) == "pending"
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-a")
    assert prov.verify(j) == "connected"
    j = connect_journey.finalize(hub, j)
    assert j["status"] == "connected" and j["finished"]
    assert (ALICE, "connect:gmail", "connected", "verified with provider") in _audit(hub)


def test_a_session_older_than_the_journey_does_not_count(hub):
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-old",
              created_at=time.time() - 3600, expires_in=7200)
    j = _started(hub, reconnect=True)
    assert providers.OwuiMcpProvider(hub, "gmail").verify(j) == "pending"
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-new")
    assert connect_journey.finalize(hub, j)["status"] == "connected"
    import sqlite3
    rows = sqlite3.connect(hub.db).execute(
        "SELECT count(*) FROM oauth_session WHERE user_id='ua'").fetchone()[0]
    assert rows == 1  # the reconnect replaced the session, no duplicate


def test_a_new_but_unusable_token_fails_the_journey(hub):
    j = _started(hub)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT",
              created_at=time.time() + 1, expires_in=-120)  # already expired, refresh impossible
    assert connect_journey.finalize(hub, j)["status"] == "failed"


def test_expiry_for_unopened_and_unfinished_journeys(hub):
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    j = store.get(hub, r["id"])
    assert connect_journey.finalize(hub, j, now=j["expires"] + 1)["status"] == "expired"
    j = _started(hub)
    assert connect_journey.finalize(hub, j, now=j["expires"] + 1)["status"] == "expired"
    assert not store.mark_started(hub, j["id"], ttl=600)  # an expired link cannot start


def test_state_changes_have_exactly_one_winner(hub):
    j = _started(hub)
    assert store.transition(hub, j["id"], frm=("started",), to="cancelled")
    assert not store.transition(hub, j["id"], frm=("started",), to="connected")
    assert store.get(hub, j["id"])["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Exactly one provider per app
# ---------------------------------------------------------------------------
def test_an_app_served_by_both_providers_is_refused_naming_both(hub):
    gate = connections.Connections(client=_broker(_FakeComposio()), allowed=["gmail"])
    connections.set_gate(gate, hub=hub.name)
    with _as(ALICE):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "conflict"
    assert "'gmail'" in err.value.message and "Composio" in err.value.message
    assert store.open_for(hub, subject=ALICE, app="gmail") == []


def test_another_hubs_gate_is_never_used(hub):
    gate = connections.Connections(client=_broker(_FakeComposio()), allowed=["github"])
    connections.set_gate(gate, hub="some-other-hub")
    with _as(ALICE, groups=("connector_github",)):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="github")
    assert err.value.code == "unavailable"


def test_two_open_webui_servers_for_one_app_are_a_conflict(hub, tmp_path, monkeypatch):
    db = tmp_path / "webui2.db"
    h.seed_owui(db, users=[("ua", ALICE)], secret=SECRET, servers=[
        {"id": "gmail", "url": "https://a/mcp"}, {"id": "Gmail", "url": "https://b/mcp"}])
    h.owui_env(monkeypatch, db, SECRET)
    with _as(ALICE):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "conflict"


def test_a_switched_off_server_is_not_offered(hub, tmp_path, monkeypatch):
    db = tmp_path / "webui3.db"
    h.seed_owui(db, users=[("ua", ALICE)], secret=SECRET, servers=[
        {"id": "gmail", "url": "https://a/mcp", "enable": False}])
    h.owui_env(monkeypatch, db, SECRET)
    with _as(ALICE):
        with pytest.raises(connect_journey.JourneyError) as err:
            connect_journey.start(hub, app="gmail")
    assert err.value.code == "unavailable"


# ---------------------------------------------------------------------------
# Composio adapter (its link is routed through the same bound journey)
# ---------------------------------------------------------------------------
class _FakeComposio:
    def __init__(self):
        self.calls = []
        self.accounts = {}  # id -> dict(status, user_id, toolkit)
        me = self

        class Accounts:
            def list(self, *, user_ids, toolkit_slugs, statuses):
                items = [{"id": i, **a} for i, a in me.accounts.items()
                         if a["user_id"] in user_ids and a["toolkit"] in toolkit_slugs
                         and a["status"] in statuses]
                return {"items": items}

            def get(self, nanoid):
                a = me.accounts.get(nanoid)
                return None if a is None else {"id": nanoid, "status": a["status"],
                                               "user_id": a["user_id"],
                                               "toolkit": {"slug": a["toolkit"]}}

            def delete(self, nanoid):
                me.calls.append(("delete", nanoid))
                me.accounts.pop(nanoid, None)

        class Link:
            def create(self, *, auth_config_id, user_id, callback_url=None, **kw):
                n = f"ca_{len(me.accounts) + 1}"
                me.accounts[n] = {"status": "INITIATED", "user_id": user_id, "toolkit": "gmail"}
                me.calls.append(("link.create", user_id, callback_url))
                return types.SimpleNamespace(connected_account_id=n,
                                             redirect_url=f"https://connect.composio.dev/{n}")

        class AuthConfigs:
            def list(self, *, toolkit_slug):
                return {"items": [{"id": "ac_1", "created_at": "2026-01-01"}]}

        self.connected_accounts = Accounts()
        self.auth_configs = AuthConfigs()
        self.client = types.SimpleNamespace(link=Link())


def _broker(fake):
    return connections.ComposioBroker(fake)


@pytest.fixture
def composio_hub(hub, monkeypatch):
    monkeypatch.setenv("OWUI_NATIVE_MCP", "false")  # Composio is the one provider
    fake = _FakeComposio()
    gate = connections.Connections(client=_broker(fake), allowed=["gmail"])
    connections.set_gate(gate, hub=hub.name)
    hub.fake = fake  # type: ignore[attr-defined]
    hub.gate = gate  # type: ignore[attr-defined]
    return hub


def test_composio_link_is_created_with_our_done_page_and_kept_server_side(composio_hub):
    hub = composio_hub
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    assert r["url"].startswith("https://hub.example.org/portal/connect/")
    assert ("link.create", ALICE, r["url"] + "/done") in hub.fake.calls
    j = store.get(hub, r["id"])
    ref = json.loads(j["provider_ref"])
    assert ref["account"] == "ca_1" and ref["redirect"].startswith("https://connect.composio.dev/")
    assert "composio" not in r["url"]
    assert providers.begin_url(hub, j) == ref["redirect"]


def test_composio_verify_and_reconnect_leaves_exactly_one_account(composio_hub):
    hub = composio_hub
    hub.fake.accounts["ca_old"] = {"status": "ACTIVE", "user_id": ALICE, "toolkit": "gmail"}
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail", reconnect=True)
    store.mark_started(hub, r["id"], ttl=600)
    j = store.get(hub, r["id"])
    assert connect_journey.finalize(hub, j)["status"] == "started"  # INITIATED: still pending
    new_id = json.loads(j["provider_ref"])["account"]
    hub.fake.accounts[new_id]["status"] = "ACTIVE"
    assert connect_journey.finalize(hub, j)["status"] == "connected"
    active = [i for i, a in hub.fake.accounts.items() if a["status"] == "ACTIVE"]
    assert active == [new_id]  # the older account was removed


def test_composio_failed_account_fails_the_journey(composio_hub):
    hub = composio_hub
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    store.mark_started(hub, r["id"], ttl=600)
    j = store.get(hub, r["id"])
    hub.fake.accounts[json.loads(j["provider_ref"])["account"]]["status"] = "FAILED"
    assert connect_journey.finalize(hub, j)["status"] == "failed"


def test_composio_account_of_someone_else_never_counts(composio_hub):
    hub = composio_hub
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    store.mark_started(hub, r["id"], ttl=600)
    j = store.get(hub, r["id"])
    acct = hub.fake.accounts[json.loads(j["provider_ref"])["account"]]
    acct.update(status="ACTIVE", user_id=BOB)
    assert connect_journey.finalize(hub, j)["status"] == "failed"


def test_a_superseded_composio_link_is_invalidated(composio_hub):
    hub = composio_hub
    with _as(ALICE):
        first = connect_journey.start(hub, app="gmail")
        connect_journey.start(hub, app="gmail")
    old = json.loads(store.get(hub, first["id"])["provider_ref"])["account"]
    assert ("delete", old) in hub.fake.calls


def test_composio_journey_cannot_be_verified_by_another_hubs_bridge(composio_hub):
    hub = composio_hub
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    store.mark_started(hub, r["id"], ttl=600)
    connections.set_gate(connections.Connections(client=None, allowed=()), hub="other")
    j = store.get(hub, r["id"])
    hub.fake.accounts[json.loads(j["provider_ref"])["account"]]["status"] = "ACTIVE"
    assert connect_journey.finalize(hub, j)["status"] == "started"  # left for the origin
    assert connect_journey.finalize(hub, j, gate=hub.gate)["status"] == "connected"


def test_connections_require_sends_the_bound_link_when_the_journey_is_on(composio_hub):
    hub = composio_hub
    ctx = types.SimpleNamespace(settings=types.SimpleNamespace(connections=("gmail",),
                                                               composio_api_key=None),
                                hub_dir=hub, connections=None)
    conns = connections.attach(ctx, broker=_broker(hub.fake))
    assert conns.journey is not None
    with _as(ALICE):
        with pytest.raises(connections.NeedsConnection) as err:
            conns.require("gmail")
    assert err.value.link.startswith("https://hub.example.org/portal/connect/")
    assert "composio" not in err.value.tool_message


def test_connections_require_is_gated_when_the_journey_is_on(composio_hub):
    import hubzoid.access as access
    hub = composio_hub
    hub.fake.accounts["ca_1"] = {"status": "ACTIVE", "user_id": ALICE, "toolkit": "gmail"}
    ctx = types.SimpleNamespace(settings=types.SimpleNamespace(connections=("gmail",),
                                                               composio_api_key=None),
                                hub_dir=hub, connections=None)
    conns = connections.attach(ctx, broker=_broker(hub.fake))
    with _as(ALICE, surface="slack-channel"):
        with pytest.raises(connections.ConnectionUnavailable) as err:
            conns.execute("gmail", "GMAIL_FETCH")
    assert err.value.reason.startswith("surface:")
    access.store_for(hub).set_authoritative(True, hub=hub.name)
    with _as(ALICE):
        with pytest.raises(connections.ConnectionUnavailable) as err:
            conns.execute("gmail", "GMAIL_FETCH")
    assert err.value.reason == "not-permitted"


def test_connections_are_unchanged_when_the_journey_is_off(composio_hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_CONNECT_JOURNEY")
    ctx = types.SimpleNamespace(settings=types.SimpleNamespace(connections=("gmail",),
                                                               composio_api_key=None),
                                hub_dir=composio_hub, connections=None)
    conns = connections.attach(ctx, broker=_broker(composio_hub.fake))
    assert conns.journey is None
    with _as(ALICE, surface="slack-channel"):
        with pytest.raises(connections.NeedsConnection) as err:
            conns.require("gmail")
    assert err.value.link.startswith("https://connect.composio.dev/")


# ---------------------------------------------------------------------------
# Capability catalog and the tool
# ---------------------------------------------------------------------------
def test_permissions_lists_connectors_the_hub_offers(hub):
    perms = connect_journey.permissions(hub)
    assert [p["permission"] for p in perms] == ["connector_gmail"]
    assert perms[0]["sensitive"] is True and perms[0]["label"] == "Connect Gmail"
    (hub / ".env").write_text("HUBZOID_CONNECT_JOURNEY=true\nCONNECTIONS=github\n")
    assert [p["permission"] for p in connect_journey.permissions(hub)] == [
        "connector_github", "connector_gmail"]


def test_permissions_empty_for_a_hub_without_connectors(tmp_path, monkeypatch):
    monkeypatch.delenv("OWUI_NATIVE_MCP", raising=False)
    assert connect_journey.permissions(tmp_path) == []


def _tool(hub):
    from hubzoid.tools import connect_tools
    (tool,) = connect_tools.make(types.SimpleNamespace(hub_dir=hub))
    return tool


def test_tool_is_absent_unless_the_journey_is_on(hub, monkeypatch):
    from hubzoid.tools import connect_tools
    assert _tool(hub).name == "connect_account"
    monkeypatch.delenv("HUBZOID_CONNECT_JOURNEY")
    assert connect_tools.make(types.SimpleNamespace(hub_dir=hub)) == []


@pytest.mark.asyncio
async def test_tool_links_the_trusted_caller_whatever_the_model_says(hub):
    tool = _tool(hub)
    assert set(tool.params_json_schema["properties"]) == {"app", "reconnect"}
    with _as(ALICE):
        out = await tool.on_invoke_tool(None, json.dumps({"app": "gmail", "user": BOB,
                                                          "subject": BOB}))
    assert "https://hub.example.org/portal/connect/" in out and ALICE in out
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    assert store.open_for(hub, subject=BOB, app="gmail") == []
    assert j["subject"] == ALICE


@pytest.mark.asyncio
async def test_tool_reports_refusals_without_internals(hub):
    tool = _tool(hub)
    with _as(ALICE, surface="slack-channel"):
        out = await tool.on_invoke_tool(None, json.dumps({"app": "gmail"}))
    assert "not available on this channel" in out
    with _as(ALICE):
        out = await tool.on_invoke_tool(None, "not json")
    assert "arguments" in out


# ---------------------------------------------------------------------------
# Continuation store
# ---------------------------------------------------------------------------
def test_continuation_attaches_only_to_this_turns_journey_and_is_single_use(hub):
    before = time.time() - 1
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    assert not connect_journey.attach_continuation(
        hub, subject=ALICE, surface="whatsapp", chat_id="whatsapp-other", since=before, text="x")
    assert not connect_journey.attach_continuation(
        hub, subject=ALICE, surface="whatsapp", chat_id=CHAT, since=time.time() + 5, text="x")
    assert connect_journey.attach_continuation(
        hub, subject=ALICE, surface="whatsapp", chat_id=CHAT, since=before, text="summarise my inbox")
    # Not offered yet (not connected): nothing to take.
    assert connect_journey.take_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT) is None
    store.transition(hub, r["id"], frm=store.OPEN, to="connected")
    store.claim_notify(hub, r["id"])
    assert store.offer_continuation(hub, r["id"]) == "summarise my inbox"
    assert connect_journey.take_continuation(hub, subject=BOB, surface="whatsapp", chat_id=CHAT) is None
    assert connect_journey.take_continuation(
        hub, subject=ALICE, surface="whatsapp", chat_id=CHAT) == "summarise my inbox"
    assert connect_journey.take_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT) is None
    assert store.get(hub, r["id"])["continuation"] is None  # the text is not kept


def test_an_offer_expires_and_a_decline_clears_it(hub):
    before = time.time() - 1
    with _as(ALICE):
        r = connect_journey.start(hub, app="gmail")
    connect_journey.attach_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT,
                                        since=before, text="do the thing")
    store.transition(hub, r["id"], frm=store.OPEN, to="connected")
    store.claim_notify(hub, r["id"], now=time.time() - store.OFFER_SECONDS - 5)
    store.offer_continuation(hub, r["id"])
    assert connect_journey.take_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT) is None
    store.expire_offers(hub, hub=hub.name)
    assert store.get(hub, r["id"])["continuation_status"] == "expired"

    with _as(ALICE):
        r2 = connect_journey.start(hub, app="gmail", reconnect=True)
    connect_journey.attach_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT,
                                        since=before, text="again")
    store.transition(hub, r2["id"], frm=store.OPEN, to="connected")
    store.claim_notify(hub, r2["id"])
    store.offer_continuation(hub, r2["id"])
    assert connect_journey.decline_continuation(hub, subject=ALICE, surface="whatsapp", chat_id=CHAT) == 1
    j = store.get(hub, r2["id"])
    assert j["continuation_status"] == "declined" and j["continuation"] is None


def test_public_base_prefers_the_public_root(monkeypatch):
    monkeypatch.delenv("WEBUI_URL", raising=False)
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.org/b/mail-hub")
    assert connect_journey.public_base() == "https://hub.example.org"
    monkeypatch.setenv("WEBUI_URL", "https://chat.example.org/")
    assert connect_journey.public_base() == "https://chat.example.org"
