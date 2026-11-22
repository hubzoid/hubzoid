"""Slack open mode ("Anyone in this Slack workspace"): the setting and its
binding, who counts as a member, email matching, Slack guests' access, and the
guests' daily cap. Slack's Web API is a fake; no model runs."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from slack_sdk.errors import SlackApiError

from hubzoid.access.identity import AppScope, Identity
from hubzoid.channels import admission, config, open_mode, store
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.slack import adapter as slack
from tests.test_channels_slack import FakeWeb, conn_for, event, hub_dir  # noqa: F401


class MemberWeb(FakeWeb):
    """Slack with a member directory (users.info) and ephemeral posts."""

    def __init__(self):
        super().__init__()
        self.people: dict[str, dict] = {}
        self.ephemeral: list[dict] = []
        self.users_info_error = None
        self.lookups = 0

    def users_info(self, user):
        self.lookups += 1
        if self.users_info_error:
            raise SlackApiError("refused", {"ok": False, "error": self.users_info_error})
        if user not in self.people:
            raise SlackApiError("refused", {"ok": False, "error": "user_not_found"})
        return {"ok": True, "user": self.people[user]}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True, "message_ts": f"{1700000100 + len(self.ephemeral)}.000100"}

    def person(self, uid, *, email=None, confirmed=True, team="T0TEAM", **flags):
        self.people[uid] = {"id": uid, "team_id": team, "name": uid.lower(), "is_email_confirmed": confirmed,
                            "profile": {"email": email, "display_name": uid.title()}, **flags}


@pytest.fixture
def web(monkeypatch):
    fake = MemberWeb()
    monkeypatch.setattr(slack, "client", lambda conn, writes=False: fake)
    open_mode.clear_cache()
    yield fake
    open_mode.clear_cache()


def _event(conn, user="U0ANA", channel="C0OPS", **extra):
    body = event(user=user, channel=channel, **extra)
    verified = conn.adapter._verified(conn, body, b"{}", method="test")
    return conn.adapter.parse(conn, verified)[0]


def _account(hub_dir, email, *, status="active", hub_access=True):
    from hubzoid.access import store_for
    from hubzoid.auth import users

    users.create(hub_dir, email=email, name=email.split("@")[0], status=status)
    if hub_access:
        store_for(hub_dir).grant(email, hub_dir.name, "use_hub", actor="test")


def _open(conn, **kw):
    return open_mode.set_open(conn, enabled=True, actor="owner@example.org", **kw)


# -- the setting ------------------------------------------------------------------------

def test_off_by_default_and_on_is_recorded(hub_dir, web):
    conn = conn_for(hub_dir)
    assert open_mode.active(conn) is None and open_mode.view(conn)["enabled"] is False
    out = _open(conn, daily_cap=5)
    assert out["enabled"] and out["daily_cap"] == 5 and out["workspace"] == "T0TEAM"
    from hubzoid.access import store_for

    with store_for(hub_dir).engine.connect() as c:
        from sqlalchemy import text

        actions = [r[0] for r in c.execute(text("SELECT action FROM hz_access_audit WHERE subject=:s"),
                                           {"s": "channel:slack-main"})]
    assert actions == ["channel_open_on"]


@pytest.mark.parametrize("cap", [0, 501])
def test_cap_bounds(hub_dir, web, cap):
    with pytest.raises(ChannelError):
        _open(conn_for(hub_dir), daily_cap=cap)


def test_only_agent_connections_of_providers_that_offer_it(hub_dir, web):
    real = conn_for(hub_dir)

    def like(**over):
        fields = dict(adapter=real.adapter, config=real.config, enabled=True, settings=real.settings,
                      hub_dir=real.hub_dir, hub=real.hub, id=real.id)
        return SimpleNamespace(**{**fields, **over})

    workflow, other = like(config=SimpleNamespace(agent=None)), like(adapter=SimpleNamespace(open_mode=False))
    for conn in (workflow, other):
        with pytest.raises(ChannelError):
            _open(conn)
        assert open_mode.active(conn) is None


def test_approval_is_bound_to_the_workspace(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    data = config.export(hub_dir, conn.id)["config"]
    data["provider_config"]["installation_team_id"] = "T0OTHER"
    v = config.load_text(hub_dir, config.render({"version": 1, **data}))
    config.apply(hub_dir, v.data, expected_revision=conn.revision, actor="hub-admin@example.org")
    moved = config.active(hub_dir, conn.id)
    view = open_mode.view(moved)
    assert view["stale"] and not view["enabled"] and open_mode.active(moved) is None
    assert open_mode.guest_access(hub_dir, conn.id) is None


# -- who counts ---------------------------------------------------------------------------

def test_off_means_unlinked(hub_dir, web):
    conn = conn_for(hub_dir)
    web.person("U0ANA")
    assert open_mode.resolve(conn, _event(conn), None) == (None, "identity_unmapped")


@pytest.mark.parametrize("flags,reason", [
    ({"is_restricted": True}, "identity_unmapped"),          # Slack's own multi-channel guest
    ({"is_ultra_restricted": True}, "identity_unmapped"),    # single-channel guest
    ({"is_stranger": True}, "identity_unmapped"),
    ({"deleted": True}, "identity_unmapped"),
    ({"is_bot": True}, "identity_unmapped"),
    ({"team": "T0OTHER"}, "identity_unmapped"),
])
def test_only_full_members_of_the_workspace(hub_dir, web, flags, reason):
    conn = conn_for(hub_dir)
    _open(conn)
    web.person("U0ANA", **flags)
    assert open_mode.resolve(conn, _event(conn), None) == (None, reason)


def test_an_outsider_team_field_is_refused_before_any_lookup(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    ev = _event(conn, user_team="T0OTHER")
    assert ev.ignore_reason == "shared_channel_denied"          # intake already refuses it
    assert open_mode.resolve(conn, ev, None) == (None, "identity_unmapped") and web.lookups == 0


def test_a_slack_outage_waits(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    web.users_info_error = "ratelimited"
    assert open_mode.resolve(conn, _event(conn), None) == (None, "wait")


def test_unmatched_member_is_a_guest(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn, daily_cap=3)
    web.person("U0ANA", email="stranger@example.org")
    person, reason = open_mode.resolve(conn, _event(conn), None)
    assert reason is None and person.guest and person.cap == 3
    assert open_mode.is_guest_subject(person.id) and "@" not in person.id


def test_confirmed_email_runs_as_the_account(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    _account(hub_dir, "ana@example.org")
    web.person("U0ANA", email="Ana@Example.org")
    person, reason = open_mode.resolve(conn, _event(conn), None)
    assert reason is None and person.open and person.email == "ana@example.org" and person.gate == "slack-channel"
    dm = _event(conn, etype="message", channel="D0ANA", channel_type="im", text="hi")
    assert open_mode.resolve(conn, dm, None)[0].gate == "slack-dm"


def test_unconfirmed_email_is_a_guest(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    _account(hub_dir, "ana@example.org")
    web.person("U0ANA", email="ana@example.org", confirmed=False)
    assert open_mode.resolve(conn, _event(conn), None)[0].guest


@pytest.mark.parametrize("setup,reason", [
    ("pending", "account_inactive"),
    ("no_access", "hub_access_denied"),
    ("blocked", "blocked"),
    ("blocked_no_account", "blocked"),
])
def test_matched_accounts_are_never_downgraded(hub_dir, web, setup, reason):
    from hubzoid.access import store_for

    conn = conn_for(hub_dir)
    _open(conn)
    if setup == "pending":
        _account(hub_dir, "ana@example.org", status="pending")
    elif setup == "no_access":
        _account(hub_dir, "ana@example.org", hub_access=False)
    elif setup == "blocked":
        _account(hub_dir, "ana@example.org")
        store_for(hub_dir).suspend("ana@example.org", actor="test")
    else:
        store_for(hub_dir).suspend("ana@example.org", actor="test")
    web.person("U0ANA", email="ana@example.org")
    assert open_mode.resolve(conn, _event(conn), None) == (None, reason)


# -- guests' access -------------------------------------------------------------------------

def _guest(cid="slack-main", tools=open_mode.GUEST_TOOLS, hub="ops"):
    return AppScope(app_id=f"channel:{cid}", agents=frozenset({hub}), tools=frozenset(tools))


def test_guest_access_follows_the_setting(hub_dir, web):
    from hubzoid.access.evaluator import app_access

    conn = conn_for(hub_dir)
    assert not app_access(hub_dir, _guest(), "use_hub").allowed            # off
    _open(conn)
    assert app_access(hub_dir, _guest(), "use_hub").allowed
    assert app_access(hub_dir, _guest(), tool="read_knowledge").allowed
    for refused in (dict(tool="write_hub_file"), dict(tool="run_workflow"), dict(tool="Bash"),
                    dict(tool="read_knowledge", server="erp"), dict(tool="search", connector="gmail")):
        assert app_access(hub_dir, _guest(), **refused).reason == "app_tool"
    for permission in ("ledger", "manage_access", "channel_slack-main", "connector_gmail"):
        assert not app_access(hub_dir, _guest(), permission).allowed
    open_mode.set_open(conn, enabled=False, actor="owner@example.org")
    assert app_access(hub_dir, _guest(), tool="read_knowledge").reason == "credential_ended"


def test_a_guest_subject_never_reads_grants(hub_dir, web):
    from hubzoid.access import store_for
    from hubzoid.access.evaluator import authorize, check_run

    store_for(hub_dir).grant("*", hub_dir.name, "use_hub", actor="test", carry_over_public=True)
    subject = open_mode.guest_subject("slack-main", "T0TEAM", "U0ANA")
    assert not authorize(hub_dir, subject, "use_hub", hub=hub_dir.name).allowed
    assert not check_run(hub_dir, Identity.make(subject, (), "slack-guest")).allowed


def test_guest_identity_without_open_mode_has_nothing(hub_dir, web):
    conn = conn_for(hub_dir)
    subject = open_mode.guest_subject(conn.id, "T0TEAM", "U0ANA")
    assert open_mode.guest_identity(hub_dir, conn.id, subject).app.tools == frozenset()
    _open(conn)
    ident = open_mode.guest_identity(hub_dir, conn.id, subject)
    assert ident.surface == "slack-guest" and ident.app.tools == open_mode.GUEST_TOOLS
    assert open_mode.guest_identity(hub_dir, conn.id, "ana@example.org").app.tools == frozenset()


def test_a_local_tool_with_a_built_in_name_is_not_a_guest_tool():
    from hubzoid.access import guard
    from hubzoid.access.identity import identity_scope

    builtin = SimpleNamespace(name="read_knowledge", hubzoid_builtin=True)
    local = SimpleNamespace(name="read_knowledge")
    mcp = SimpleNamespace(name="read_knowledge", hubzoid_builtin=True, hubzoid_server="erp")
    with identity_scope(Identity.make("slackguest_x", (), "slack-guest", app=_guest())):
        assert guard.visible(builtin) and not guard.visible(local) and not guard.visible(mcp)
        assert guard.visible_map({"read_knowledge": local}) == {"read_knowledge": False}


def test_built_in_tools_carry_the_marker_through_the_real_wrappers(hub_dir):
    from hubzoid import settings
    from hubzoid.access import guard
    from hubzoid.tool_classes import classify
    from hubzoid.tools import make_all

    (hub_dir / "knowledge").mkdir(exist_ok=True)
    ctx = SimpleNamespace(hub_dir=hub_dir, output_dir=hub_dir / "output", session_id="s",
                          settings=settings.load(hub_dir), skills=[], knowledge=[], delegates=[], connections=None)
    tools = guard.apply(hub_dir, make_all(ctx))          # restricted guards and run_checked, as every factory
    assert open_mode.GUEST_TOOLS <= set(tools)
    assert all(getattr(t, "hubzoid_builtin", False) for t in tools.values())
    assert {classify(tools[n]) for n in open_mode.GUEST_TOOLS} == {"read"}


def test_the_call_time_wall_refuses_a_guest_a_tool_off_the_list(hub_dir, web):
    import asyncio
    import dataclasses

    from agents.tool import FunctionTool

    from hubzoid.access import guard
    from hubzoid.access.identity import identity_scope

    async def ran(_ctx, _args):
        return "ran"

    def tool(name, builtin):
        ft = FunctionTool(name=name, description="", params_json_schema={"type": "object", "properties": {}},
                          on_invoke_tool=ran)
        if builtin:
            ft.hubzoid_builtin = True
        return guard.run_checked(ft, hub_dir)

    conn = conn_for(hub_dir)
    _open(conn)
    subject = open_mode.guest_subject(conn.id, "T0TEAM", "U0ANA")
    with identity_scope(open_mode.guest_identity(hub_dir, conn.id, subject)):
        assert asyncio.run(tool("read_knowledge", True).on_invoke_tool(None, "{}")) == "ran"
        assert "not one of the tools" in asyncio.run(tool("read_knowledge", False).on_invoke_tool(None, "{}"))
        assert "not one of the tools" in asyncio.run(tool("write_artifact", True).on_invoke_tool(None, "{}"))
    del dataclasses


def test_a_store_error_on_the_block_check_waits_not_guest(hub_dir, web, monkeypatch):
    conn = conn_for(hub_dir)
    _open(conn)
    web.person("U0ANA", email="ana@example.org")
    monkeypatch.setattr(open_mode, "_suspended", lambda hub_dir, email: None)
    assert open_mode.resolve(conn, _event(conn), None) == (None, "wait")


def test_a_slack_outage_is_not_asked_again_at_once(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    web.users_info_error = "ratelimited"
    for _ in range(3):
        assert open_mode.resolve(conn, _event(conn), None) == (None, "wait")
    assert web.lookups == 1


def test_turning_it_on_again_for_a_new_workspace_is_an_on(hub_dir, web):
    from sqlalchemy import text

    from hubzoid.access import store_for

    conn = conn_for(hub_dir)
    _open(conn)
    data = config.export(hub_dir, conn.id)["config"]
    data["provider_config"]["installation_team_id"] = "T0OTHER"
    config.apply(hub_dir, config.load_text(hub_dir, config.render({"version": 1, **data})).data,
                 expected_revision=conn.revision, actor="hub-admin@example.org")
    _open(config.active(hub_dir, conn.id))
    with store_for(hub_dir).engine.connect() as c:
        actions = [r[0] for r in c.execute(text("SELECT action FROM hz_access_audit WHERE subject=:s ORDER BY rowid"),
                                           {"s": "channel:slack-main"})]
    assert actions == ["channel_open_on", "channel_open_on"]


# -- routing a guest and a recognised account --------------------------------------------------

class _Engine:
    """The chat engine calls the router makes, recorded."""

    def __init__(self):
        self.enqueued, self.convs = [], 0

    def actor(self, person):
        from hubzoid.chat.engine import Actor

        return Actor(id=person.id, email=person.email, credential=getattr(person, "credential", "session"),
                     credential_id=getattr(person, "credential_id", ""))

    admission = None

    def create_conversation(self, actor, **kw):
        self.convs += 1
        return {"conversation_id": f"c{self.convs}"}

    def enqueue(self, conversation_id, actor, body, *, key):
        self.enqueued.append((conversation_id, actor, body))
        return SimpleNamespace(body={"dispatched": True, "run_id": "r1"}, replayed=False), None


def _app(monkeypatch):
    from hubzoid.channels import router

    engine = _Engine()
    monkeypatch.setattr(router, "conversation_busy", lambda engine, cid: False)
    monkeypatch.setattr(router, "thread_branch", lambda engine, cid, actor_id: "b1")
    monkeypatch.setattr(router, "_open_conversation", lambda hub_dir, conn, thread, actor, chat:
                        chat.engine.create_conversation(actor)["conversation_id"])
    monkeypatch.setattr(router, "_hub_access", lambda hub_dir, email: None)
    return SimpleNamespace(state=SimpleNamespace(chat=SimpleNamespace(engine=engine, files=None))), engine


def _deliver(conn, body):
    from tests.test_channels_slack import receive

    receive(conn, body)
    return store.open_deliveries(conn.hub_dir, config.hub_of(conn.hub_dir))[-1]


def test_a_guest_is_routed_as_a_guest_with_their_cap(hub_dir, web, monkeypatch):
    from hubzoid.channels import router

    conn = conn_for(hub_dir)
    _open(conn, daily_cap=1)
    web.person("U0ANA")
    app, engine = _app(monkeypatch)
    row = _deliver(conn, event(event_id="Ev1"))
    assert router._route_one(hub_dir, row, app)[0] == "routed"
    _, actor, _body = engine.enqueued[0]
    assert actor.credential == open_mode.GUEST_CREDENTIAL and actor.credential_id == conn.id
    assert open_mode.is_guest_subject(actor.id)
    assert store.get_delivery(hub_dir, row["id"])["route_json"]["guest"] == actor.id
    second = _deliver(conn, event(event_id="Ev2", ts="1700000000.000900", thread_ts="1700000000.000900"))
    router._route_one(hub_dir, second, app)
    assert store.get_delivery(hub_dir, second["id"])["reason"] == "guest_cap"


def test_a_recognised_account_runs_with_the_slack_surface(hub_dir, web, monkeypatch):
    from hubzoid.channels import router

    conn = conn_for(hub_dir)
    _open(conn)
    _account(hub_dir, "ana@example.org")
    web.person("U0ANA", email="ana@example.org")
    app, engine = _app(monkeypatch)
    row = _deliver(conn, event())
    assert router._route_one(hub_dir, row, app)[0] == "routed"
    actor = engine.enqueued[0][1]
    assert actor.email == "ana@example.org"
    assert router.parse_channel_credential(actor.credential_id) == ("slack", "slack-channel", conn.id)


def test_run_identity_for_guests_and_channel_runs(hub_dir, web):
    from hubzoid.chat import runs

    conn = conn_for(hub_dir)
    _open(conn)
    subject = open_mode.guest_subject(conn.id, "T0TEAM", "U0ANA")
    guest = SimpleNamespace(credential=open_mode.GUEST_CREDENTIAL, credential_id=conn.id, message_id="m1",
                            email=subject, owner_id=subject, conversation_id="c1", key_hubs=None)
    record = runs._admit(hub_dir, guest)
    assert record["acts_as"] is None and record["started_from"] == "slack-guest"
    _account(hub_dir, "ana@example.org")
    from hubzoid.auth import users

    acc = users.find_by_email(hub_dir, "ana@example.org")
    linked = SimpleNamespace(credential="channel", credential_id=f"slack|web|{conn.id}", message_id="m2",
                             email="ana@example.org", owner_id=acc["id"], conversation_id="c2", key_hubs=None)
    record = runs._admit(hub_dir, linked)
    assert record["acts_as"] == acc["id"] and record["started_from"] == "slack"


# -- the daily cap ----------------------------------------------------------------------------

def _delivery(conn, n: int) -> str:
    with store.locked(conn.hub_dir, "test") as c:
        row = store.insert_delivery(c, hub=conn.hub, connection_id=conn.id, delivery_id=f"d{n}", body_digest="x",
                                    kind="message", status="routing", verification="test")
    return row["id"]


def test_cap_is_per_guest_per_day_and_idempotent(hub_dir, web):
    conn = conn_for(hub_dir)
    a, b = "slackguest_a", "slackguest_b"
    ids = [_delivery(conn, n) for n in range(4)]
    assert open_mode.reserve(conn, a, ids[0], 2) and open_mode.reserve(conn, a, ids[1], 2)
    assert open_mode.reserve(conn, a, ids[0], 2)               # a retry keeps its own reservation
    assert not open_mode.reserve(conn, a, ids[2], 2)           # the third one today
    assert open_mode.reserve(conn, b, ids[3], 2)               # someone else is unaffected


@pytest.mark.slow   # many database round trips, one per generated case
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plan=st.lists(st.tuples(st.sampled_from(["a", "b", "c"]), st.integers(0, 1), st.booleans()),
                     min_size=1, max_size=14),
       cap=st.integers(1, 3))
def test_no_guest_ever_exceeds_the_cap(hub_dir, web, monkeypatch, plan, cap):
    conn = conn_for(hub_dir) if config.active_all(hub_dir) == [] else config.active(hub_dir, "slack-main")
    with store.engine(hub_dir).begin() as c:
        from sqlalchemy import text

        c.execute(text("DELETE FROM hz_channel_deliveries"))
    clock = {"t": time.time()}
    monkeypatch.setattr(open_mode, "_day", lambda t=None: time.strftime("%Y-%m-%d", time.gmtime(clock["t"])))
    granted: dict[tuple, set] = {}
    last: dict[str, str] = {}     # a retry is the same person's own last delivery
    for n, (who, next_day, retry) in enumerate(plan):
        if next_day:
            clock["t"] += 86400
        did = last[who] if retry and who in last else _delivery(conn, n)
        last[who] = did
        if open_mode.reserve(conn, f"slackguest_{who}", did, cap):
            granted.setdefault((who, open_mode._day()), set()).add(did)
    assert all(len(v) <= cap for v in granted.values())


# -- the API ------------------------------------------------------------------------------------

@pytest.mark.parametrize("org_admin,status", [(False, 403), (True, 200)])
def test_only_an_organization_administrator_changes_it(hub_dir, web, monkeypatch, org_admin, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hubzoid.channels import routes
    from hubzoid.control import authz

    conn_for(hub_dir)

    async def fake_require(request, hub_dir_, capability, hub=None, *, mutation=False):
        return SimpleNamespace(email="someone@example.org", org_admin=org_admin)

    monkeypatch.setattr(authz, "require_request", fake_require)
    app = FastAPI()
    routes.mount(app, hub_dir)
    client = TestClient(app)
    base = f"/api/hubs/{config.hub_of(hub_dir)}/channels/slack-main/open"
    r = client.put(base, json={"enabled": True, "daily_cap": 7})
    assert r.status_code == status
    view = client.get(base).json()
    assert view["enabled"] is org_admin and view["can_change"] is org_admin


def test_manifest_asks_for_email_only_while_open(hub_dir, web):
    from hubzoid.channels.slack import manifest

    conn = conn_for(hub_dir)
    assert "users:read.email" not in manifest.build(conn)["oauth_config"]["scopes"]["bot"]
    _open(conn)
    assert "users:read.email" in manifest.build(conn)["oauth_config"]["scopes"]["bot"]


def test_doctor_warns_while_open(hub_dir, web):
    conn = conn_for(hub_dir)
    _open(conn)
    checks = {c.id: c for c in conn.adapter.doctor(conn, {})}
    assert checks["slack:open_mode"].status == "warn" and checks["slack:email_scope"].status == "warn"


del admission  # imported for fixture discovery parity with the Slack tests


# -- approvals ---------------------------------------------------------------------------------

def test_a_guest_never_decides_a_request(hub_dir):
    from hubzoid.hitl import service

    subject = open_mode.guest_subject("slack-main", "T0TEAM", "U0ANA")
    with pytest.raises(service.RequestError) as caught:
        service.decide(hub_dir, "ops", "any-request", account_id=subject, email=subject, decision="approve")
    assert caught.value.status == 403


def test_a_guest_request_is_never_continued():
    import asyncio

    from hubzoid.chat.engine_routes import Orchestrator

    request = {"origin": "chat", "conversation_id": "c1", "status": "approved",
               "requester_id": open_mode.guest_subject("slack-main", "T0TEAM", "U0ANA")}
    # Returns before touching anything else (``self`` is never used).
    assert asyncio.run(Orchestrator.continue_request(None, request)) is False
