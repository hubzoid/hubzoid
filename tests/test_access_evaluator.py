"""One access check (``access.evaluator``) and the per-tool-call recheck: a
grant, a block, a deleted account or revoked credentials apply on the very
next check, on every runtime's tools, with no admin bypass and no prompt."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from agents import function_tool

from hubzoid import access
from hubzoid.access import evaluator, run_identity
from hubzoid.access.identity import Identity, identity_scope
from hubzoid.auth import users

from tests.access_helpers import allow
from tests.test_auth_mfa import hub  # noqa: F401 - fixture
from tests.test_auth_routes import person


@function_tool
def lookup(q: str) -> str:
    """Look something up."""
    return f"found {q}"


def _call(tool, ident) -> str:
    from agents import RunConfig
    from agents.tool_context import ToolContext

    args = json.dumps({"q": "x"})
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t1", tool_arguments=args,
                      run_config=RunConfig())

    async def go():
        with identity_scope(ident):
            return await tool.on_invoke_tool(ctx, args)
    return asyncio.run(go())


def test_grant_and_revoke_apply_on_the_next_check(hub):
    person(hub)
    assert not evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name)
    allow(hub, "ana@example.com")
    assert evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name)
    access.store_for(hub).revoke("ana@example.com", hub.name, "use_hub", actor="test")
    decision = evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name)
    assert not decision and decision.reason == "missing_grant"


def test_blocked_and_deleted_people_are_refused(hub):
    ana = person(hub)
    boss = person(hub, email="boss@example.com", role="admin")
    access.store_for(hub).grant("boss@example.com", "*", "manage_access", actor="test")
    allow(hub, "ana@example.com")
    access.store_for(hub).suspend("ana@example.com", actor="test")
    assert evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name).reason == "blocked"
    users.delete(hub, ana["id"])
    gone = evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name,
                               account_id=ana["id"])
    assert gone.reason == "account_inactive"
    assert boss  # the org admin's own access is a grant, checked the same way
    assert evaluator.authorize(hub, "boss@example.com", "admin.config.read")


def test_no_admin_bypass_for_hub_tools(hub):
    person(hub, email="boss@example.com", role="admin")
    # An administrator role is not a grant: without use_hub there is no access.
    assert not evaluator.authorize(hub, "boss@example.com", "use_hub", hub=hub.name)


def test_store_error_refuses(hub, monkeypatch):
    person(hub)
    allow(hub, "ana@example.com")
    monkeypatch.setattr(access, "store_for", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()))
    assert evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name).reason == "store_error"


def test_every_tool_rechecks_the_runs_account(hub):
    ana = person(hub)
    allow(hub, "ana@example.com")
    record = run_identity.admit(hub, run_kind="chat", run_id="m_1", acts_as_id=ana["id"],
                                acts_as_email=ana["email"], person_present=True)
    ident = run_identity.identity(record, "web")
    tool = access.apply(hub, {"lookup": lookup})["lookup"]
    assert _call(tool, ident) == "found x"  # allowed: it just runs, nothing asks
    users.revoke_credentials(hub, ana["id"])
    assert "access ended" in _call(tool, ident)  # the same run, its next tool call


def test_revoking_the_runs_own_key_or_device_stops_it(hub):
    from hubzoid.auth import api_keys, sessions
    from hubzoid.auth import AuthUser

    ana = person(hub)
    allow(hub, "ana@example.com")
    owner = users.store(hub).get(ana["id"])
    key, _ = api_keys.create(hub, AuthUser(id=ana["id"], email=ana["email"], name="Ana",
                                           role="user"), name="s", operations=["chat"])
    token = sessions.create_session(hub, owner, method="password")
    sid = sessions.session_id(sessions.digest(token))
    tool = access.apply(hub, {"lookup": lookup})["lookup"]
    runs = {}
    for kind, cid in (("api_key", key["id"]), ("session", sid)):
        record = run_identity.admit(hub, run_kind="chat", run_id=f"m_{kind}", acts_as_id=ana["id"],
                                    acts_as_email=ana["email"], credential_kind=kind,
                                    credential_id=cid)
        runs[kind] = run_identity.identity(record, "web")
        assert _call(tool, runs[kind]) == "found x"
    api_keys.revoke(hub, ana["id"], key["id"])
    assert "access ended" in _call(tool, runs["api_key"])
    assert _call(tool, runs["session"]) == "found x"  # only that key's runs stop
    assert sessions.revoke_session_id(hub, ana["id"], sid)
    assert "access ended" in _call(tool, runs["session"])


def test_outside_mcp_servers_get_the_same_check(hub):
    """A hub's or a person's MCP server on Codex and the Agents SDK: the call
    reaches ``server.call_tool``, which checks the run first."""
    from hubzoid.access.guard import checked_server

    class Server:
        calls = 0

        async def call_tool(self, name, arguments, meta=None):
            Server.calls += 1
            return "ran"

    ana = person(hub)
    allow(hub, "ana@example.com")
    record = run_identity.admit(hub, run_kind="chat", run_id="m_mcp", acts_as_id=ana["id"],
                                acts_as_email=ana["email"])
    ident = run_identity.identity(record, "web")
    server = checked_server(Server(), hub)

    async def call():
        with identity_scope(ident):
            return await server.call_tool("search", {})
    assert asyncio.run(call()) == "ran"
    users.revoke_credentials(hub, ana["id"])
    refused = asyncio.run(call())
    assert refused.isError and "access ended" in refused.content[0].text and Server.calls == 1


def test_anonymous_calls_keep_their_old_behaviour(hub):
    tool = access.apply(hub, {"lookup": lookup})["lookup"]
    assert _call(tool, access.ANONYMOUS) == "found x"


def test_decisions_are_fast_once_warm(hub):
    ana = person(hub)
    allow(hub, "ana@example.com")
    ident = Identity.make(ana["email"], surface="web", account_id=ana["id"], auth_epoch=0)
    evaluator.check_run(hub, ident)
    started = time.perf_counter()
    for _ in range(200):
        assert evaluator.check_run(hub, ident)
    per_call = (time.perf_counter() - started) / 200
    assert per_call < 0.01, f"{per_call * 1000:.2f} ms per check"


@pytest.mark.parametrize("kind", ["chat", "schedule", "workflow", "mcp", "api"])
def test_run_identity_is_written_once_and_reads_plainly(hub, kind):
    ana = person(hub)
    first = run_identity.admit(hub, run_kind=kind, run_id="r1", acts_as_id=ana["id"],
                               acts_as_email=ana["email"], started_from=kind,
                               person_present=kind == "chat")
    again = run_identity.admit(hub, run_kind=kind, run_id="r1", acts_as_email="mallory@example.com")
    assert again["id"] == first["id"] and again["acts_as_email"] == ana["email"]
    assert first["auth_epoch"] == 0 and first["acts_as"] == ana["id"]
    text = run_identity.describe(first, {ana["email"]: "Ana"})
    assert "Ana" in text
    if kind == "schedule":
        assert text.startswith("Scheduled run for Ana")


def test_a_whatsapp_message_runs_as_the_phones_account(hub):
    """Phone -> account -> access: the bridge records the channel run against
    the account, so revoking that account's credentials stops its tools too."""
    from hubzoid.server import _channel_identity

    ana = person(hub)
    allow(hub, "ana@example.com")
    sender = Identity.make("ana@example.com", surface="whatsapp")  # resolved from her phone
    ident = _channel_identity(hub, sender, "whatsapp-15550001")
    record = run_identity.get(hub, ident.run_identity_id)
    assert record["acts_as"] == ana["id"] and record["person_present"]
    assert run_identity.describe(record, {"ana@example.com": "Ana"}) == "Started by Ana from WhatsApp"
    tool = access.apply(hub, {"lookup": lookup})["lookup"]
    assert _call(tool, ident) == "found x"
    users.revoke_credentials(hub, ana["id"])
    assert "access ended" in _call(tool, ident)


def test_legacy_runs_say_so(hub):
    record = run_identity.admit(hub, run_kind="workflow", run_id="old", acts_as_email=None,
                                requested_by="workflow:nightly", legacy=True)
    assert record["legacy"] and "Older run" in run_identity.describe(record)


def test_a_reader_marker_ends_access_from_everyone_signed_in(hub):
    """Someone a report link brought in counts only grants made to them by
    name, even when "everyone signed in" opened the agent a moment before."""
    person(hub)
    gs = access.store_for(hub)
    gs.grant("*", hub.name, "use_hub", actor="test", carry_over_public=True)
    assert evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name)
    gs.set_reader("ana@example.com")
    assert evaluator.authorize(hub, "ana@example.com", "use_hub", hub=hub.name).reason == "missing_grant"


# -- property: the decision cache never changes a decision -----------------------
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_OPS = ("grant", "revoke", "block", "reactivate", "reader", "not_reader", "pending", "active",
        "unavailable", "available", "revoke_credentials", "everyone")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=st.lists(st.tuples(st.sampled_from(_OPS), st.integers(0, 1)), min_size=1, max_size=8))
def test_the_cache_never_changes_a_decision(hub, ops):
    """After any sequence of access changes, a check answered from the cache is
    the same as one worked out afresh."""
    import uuid

    gs = access.store_for(hub)
    tag = uuid.uuid4().hex[:8]
    people = [users.create(hub, email=f"p{i}-{tag}@example.com", name="P") for i in range(2)]
    for p in people:
        users.sync_identity(hub, p)

    def decide(p):
        return evaluator.authorize(hub, p["email"], "use_hub", hub=hub.name, account_id=p["id"])

    for op, i in ops:
        p = people[i]
        for q in people:
            decide(q)  # warm the cache with the state before the change
        if op == "grant":
            gs.grant(p["email"], hub.name, "use_hub", actor="test")
        elif op == "revoke":
            gs.revoke(p["email"], hub.name, "use_hub", actor="test")
        elif op in ("block", "reactivate"):
            gs.suspend(p["email"], actor="test", suspended=op == "block")
        elif op in ("reader", "not_reader"):
            gs.set_reader(p["email"], op == "reader")
        elif op in ("pending", "active"):
            users.set_status(hub, p["id"], op)
        elif op in ("unavailable", "available"):
            gs.upsert_identity(email=p["email"], owui_id=p["id"], pending=op == "unavailable")
        elif op == "revoke_credentials":
            users.revoke_credentials(hub, p["id"])
        elif op == "everyone" and not gs.everyone_can_use(hub.name):
            gs.grant("*", hub.name, "use_hub", actor="test", carry_over_public=True)
        for q in people:
            cached = decide(q)
            evaluator.clear_cache()
            fresh = decide(q)
            assert (cached.allowed, cached.reason) == (fresh.allowed, fresh.reason), (op, q["email"])
