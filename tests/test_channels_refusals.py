"""Every refusal on a channel connection tells the person who asked, once, in
private where the provider allows it: refusals decided at intake (a channel the
bot does not answer in, DMs off, a full queue), the router's refusals, and a
notice a rate limit held back. Plus the store hiccups that must wait instead of
refusing, Socket Mode recovery and the usage surface of channel runs."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from slack_sdk.errors import SlackApiError

from hubzoid.channels import config, router, store
from hubzoid.channels.slack import adapter as slack
from tests import test_channels_contract as cc
from tests.test_channels_contract import fake  # noqa: F401
from tests.test_channels_open_mode import MemberWeb
from tests.test_channels_slack import conn_for, event, hub_dir, receive  # noqa: F401


@pytest.fixture
def web(monkeypatch):
    w = MemberWeb()
    monkeypatch.setattr(slack, "client", lambda conn, writes=False: w)
    return w


def _rows(hub_dir):
    return store.list_deliveries(hub_dir, config.hub_of(hub_dir), "slack-main")["items"]


# -- refusals decided at intake --------------------------------------------------------------

def test_a_mention_in_a_channel_not_allowed_gets_a_private_notice_once(hub_dir, web):
    conn = conn_for(hub_dir)
    receive(conn, event(channel="C0OTHER", event_id="Ev1"))
    receive(conn, event(channel="C0OTHER", event_id="Ev1"))                 # Slack's retry
    receive(conn, event(etype="message", channel="C0OTHER", event_id="Ev2", channel_type="channel"))
    assert [r["status"] for r in _rows(hub_dir)].count("ignored") >= 1
    for _ in range(3):
        router.post_owed_notices(hub_dir)
    assert len(web.ephemeral) == 1 and web.posts == []
    note = web.ephemeral[0]
    assert note["user"] == "U0ANA" and note["channel"] == "C0OTHER" and "thread_ts" not in note
    assert "don't answer in this channel" in note["text"]


def test_inside_an_existing_thread_the_private_notice_stays_in_the_thread(hub_dir, web):
    conn = conn_for(hub_dir)
    receive(conn, event(channel="C0OTHER", ts="1700000000.000200", thread_ts="1700000000.000100"))
    router.post_owed_notices(hub_dir)
    assert web.ephemeral[0]["thread_ts"] == "1700000000.000100"


def test_dms_off_is_told_in_the_dm(hub_dir, web):
    conn = conn_for(hub_dir, allow_direct_messages=False)
    receive(conn, event(etype="message", channel="D0ANA", text="hello", channel_type="im"))
    router.post_owed_notices(hub_dir)
    assert web.ephemeral == [] and "Direct messages to me are turned off" in web.posts[0]["text"]


def test_with_dms_off_the_channel_notice_does_not_suggest_a_dm(hub_dir, web):
    conn = conn_for(hub_dir, allow_direct_messages=False)
    receive(conn, event(channel="C0OTHER"))
    router.post_owed_notices(hub_dir)
    assert "Message me directly" not in web.ephemeral[0]["text"]


@pytest.mark.parametrize("case", ["bot", "slack_connect", "foreign_team", "passing_remark"])
def test_silent_cases_owe_nothing(hub_dir, web, case):
    conn = conn_for(hub_dir)
    body = {"bot": event(channel="C0OTHER", bot_id="B9"),
            "slack_connect": event(is_ext_shared_channel=True),
            "foreign_team": event(etype="message", channel="D0EXT", text="hi", channel_type="im",
                                  user_team="T0OTHER"),
            "passing_remark": event(etype="message", channel="C0OTHER", text="no mention",
                                    channel_type="channel")}[case]
    receive(conn, body)
    assert router.post_owed_notices(hub_dir) == 0 and web.posts == [] and web.ephemeral == []
    if case == "foreign_team":
        assert _rows(hub_dir)[0]["reason"] == "shared_channel_denied"


def test_a_paused_connection_stays_silent(hub_dir, web):
    conn = conn_for(hub_dir)
    config.set_enabled(hub_dir, conn.id, False, expected_revision=conn.revision, actor="test")
    receive(config.active(hub_dir, conn.id), event(channel="C0OTHER"))
    assert router.post_owed_notices(hub_dir) == 0 and web.ephemeral == []


def test_a_full_thread_queue_is_told(fake):  # noqa: F811
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "desk"
        root.mkdir()
        (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
        import os

        os.environ["FAKE_CHANNEL_SECRET"] = cc.SECRET
        conn = cc.applied(root, queue_limit=1)
        for n in range(2):
            cc.send(conn, cc.ev(eid=f"e{n}"), delivery_id=f"d{n}")
        router.post_owed_notices(root)
        assert [s["text"] for s in fake.sent] == [router.NOTICES["queue_full"]]


# -- notices and rate limits -------------------------------------------------------------------

def test_a_rate_limited_notice_is_posted_on_a_later_pass(hub_dir, web, monkeypatch):
    conn = conn_for(hub_dir)
    receive(conn, event(channel="C0OTHER"))
    calls = {"n": 0}
    original = web.chat_postEphemeral

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SlackApiError("slow down", {"ok": False, "error": "ratelimited"})
        return original(**kw)

    monkeypatch.setattr(web, "chat_postEphemeral", flaky)
    assert router.post_owed_notices(hub_dir) == 0           # held back
    assert router.post_owed_notices(hub_dir) == 1 and len(web.ephemeral) == 1
    assert router.post_owed_notices(hub_dir) == 0           # never twice


@pytest.mark.slow   # many database round trips, one per generated case
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(outcomes=st.lists(st.sampled_from(["ratelimited", "ok", "channel_not_found"]), min_size=1, max_size=6))
def test_an_owed_notice_is_posted_at_most_once(hub_dir, web, monkeypatch, outcomes):
    with store.engine(hub_dir).begin() as c:
        from sqlalchemy import text

        c.execute(text("DELETE FROM hz_channel_deliveries"))
        c.execute(text("DELETE FROM hz_operations"))
    web.ephemeral.clear()
    conn = conn_for(hub_dir) if not config.active_all(hub_dir) else config.active(hub_dir, "slack-main")
    receive(conn, event(channel="C0OTHER"))
    queue = list(outcomes)
    original = MemberWeb.chat_postEphemeral

    def scripted(**kw):
        step = queue.pop(0) if queue else "ok"
        if step != "ok":
            raise SlackApiError("refused", {"ok": False, "error": step})
        return original(web, **kw)

    monkeypatch.setattr(web, "chat_postEphemeral", scripted)
    for _ in range(len(outcomes) + 2):
        router.post_owed_notices(hub_dir)
    assert len(web.ephemeral) <= 1


def test_the_unlinked_notice_names_the_slack_id_privately(hub_dir, web):
    conn = conn_for(hub_dir)
    receive(conn, event())
    router._route_one(hub_dir, store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0], None)
    assert web.posts == [] and "(your Slack id is U0ANA)" in web.ephemeral[0]["text"]


def test_files_off_is_named(hub_dir, web):
    conn = conn_for(hub_dir, files_enabled=False)
    receive(conn, event(text="<@UBOT>", subtype="file_share", files=[{"id": "F1", "name": "a.pdf"}]))
    router._route_one(hub_dir, store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0], None)
    assert "Files are turned off" in web.ephemeral[0]["text"]
    assert _rows(hub_dir)[0]["reason"] == "files_off"


# -- hiccups wait, refusals refuse -----------------------------------------------------------------

def _linked_app(monkeypatch, admission_error=None):
    from hubzoid.chat.engine import Actor

    person = SimpleNamespace(id="acc1", email="ana@example.org", name="Ana")
    monkeypatch.setattr(router, "person_for", lambda conn, event: (person, None))
    monkeypatch.setattr(router, "_hub_access", lambda hub_dir, email: None)

    def admission(actor):
        if admission_error is not None:
            raise admission_error

    engine = SimpleNamespace(actor=lambda p: Actor(id=p.id, email=p.email), admission=admission)
    return SimpleNamespace(state=SimpleNamespace(chat=SimpleNamespace(engine=engine)))


@pytest.mark.parametrize("status,code,expect", [(503, "access_unavailable", "accepted"),
                                                (403, "suspended", "denied"),
                                                (403, "no_access", "denied")])
def test_engine_admission_hiccup_waits(hub_dir, web, monkeypatch, status, code, expect):
    from hubzoid.chat.graph import GraphError

    conn = conn_for(hub_dir)
    monkeypatch.setattr(router, "_groups_ok", lambda conn, email: True)
    app = _linked_app(monkeypatch, GraphError(status, code, "x"))
    receive(conn, event())
    row = store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0]
    router._route_one(hub_dir, row, app)
    got = store.get_delivery(hub_dir, row["id"])
    assert got["status"] == expect
    if code == "suspended":
        assert got["reason"] == "blocked"


def test_a_grant_check_that_cannot_answer_waits(hub_dir, web, monkeypatch):
    from hubzoid import access

    conn = conn_for(hub_dir)
    app = _linked_app(monkeypatch)
    monkeypatch.setattr(access, "store_for", lambda hub_dir: (_ for _ in ()).throw(RuntimeError("db down")))
    receive(conn, event())
    row = store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0]
    assert router._route_one(hub_dir, row, app) == ("waiting", None)
    assert store.get_delivery(hub_dir, row["id"])["status"] == "accepted"


# -- Socket Mode recovery ---------------------------------------------------------------------------

class _Handle:
    def __init__(self, log, name, healthy=True):
        self.log, self.name, self.is_healthy = log, name, healthy

    def healthy(self):
        return self.is_healthy

    def stop(self):
        self.log.append(("stop", self.name))


def _transports(monkeypatch, hub_dir, conns, *, fail=0):
    log, made = [], []
    state = {"fail": fail}

    def start(conn, hub_dir_):
        if state["fail"]:
            state["fail"] -= 1
            raise RuntimeError("Slack unreachable")
        h = _Handle(log, conn.id)
        made.append(h)
        log.append(("start", conn.id))
        return h

    for c in conns:
        monkeypatch.setattr(c, "adapter", SimpleNamespace(start=start, secret_keys=()), raising=False)
    monkeypatch.setattr(config, "active_all", lambda hub_dir_: conns)
    monkeypatch.setattr(router._state, "hub_dir", hub_dir)
    monkeypatch.setattr(router._state, "transports", {})
    monkeypatch.setattr(router._state, "backoff", {})
    monkeypatch.setattr(router._state, "unhealthy", {})
    return log, made


def _conn(cid, enabled=True, revision=1):
    return SimpleNamespace(id=cid, enabled=enabled, revision=revision, settings=SimpleNamespace())


def test_a_failed_start_is_retried_after_a_backoff(hub_dir, monkeypatch):
    c = _conn("slack-main")
    log, _ = _transports(monkeypatch, hub_dir, [c], fail=1)
    asyncio.run(router.reload_transports())
    assert log == [] and router._state.backoff
    asyncio.run(router.reload_transports())                 # still inside the backoff
    assert log == []
    name = next(iter(router._state.backoff))
    router._state.backoff[name] = (0.0, router.BACKOFF_MIN)
    asyncio.run(router.reload_transports())
    assert log == [("start", "slack-main")] and not router._state.backoff


def test_a_new_token_value_restarts_the_transport(hub_dir, monkeypatch):
    c = _conn("slack-main")
    log, _ = _transports(monkeypatch, hub_dir, [c])
    token = {"v": "a"}
    monkeypatch.setattr(router, "_fingerprint", lambda conn: token["v"])
    asyncio.run(router.reload_transports())
    token["v"] = "b"
    asyncio.run(router.reload_transports())
    assert log == [("start", "slack-main"), ("stop", "slack-main"), ("start", "slack-main")]


def test_a_disconnected_transport_is_started_again(hub_dir, monkeypatch):
    c = _conn("slack-main")
    log, made = _transports(monkeypatch, hub_dir, [c])
    asyncio.run(router.reload_transports())
    made[0].is_healthy = False
    asyncio.run(router.reload_transports())                 # first seen down: give it a moment
    assert log == [("start", "slack-main")]
    name = next(iter(router._state.unhealthy))
    router._state.unhealthy[name] -= router.BACKOFF_MIN + 1
    asyncio.run(router.reload_transports())
    assert log == [("start", "slack-main"), ("stop", "slack-main"), ("start", "slack-main")]


def test_a_disabled_connection_is_stopped(hub_dir, monkeypatch):
    c = _conn("slack-main")
    log, _ = _transports(monkeypatch, hub_dir, [c])
    asyncio.run(router.reload_transports())
    c.enabled = False
    asyncio.run(router.reload_transports())
    assert log == [("start", "slack-main"), ("stop", "slack-main")]


# -- usage under the channel ----------------------------------------------------------------------------

def test_a_channel_run_is_counted_under_its_channel(monkeypatch):
    from hubzoid import access, server
    from hubzoid.chat import runs

    seen = []

    async def record(hub_dir, identity, *_a):
        seen.append(identity.surface)

    monkeypatch.setattr(server, "_record_turn", record)
    mgr = runs.RunManager(SimpleNamespace(hub_dir="/tmp/x"))
    channel = SimpleNamespace(credential=router.CHANNEL_CREDENTIAL, credential_id="slack|web|slack-main",
                              email="ana@example.org", conversation_id="c1", started=0.0)
    web_run = SimpleNamespace(credential="session", credential_id="", email="ana@example.org",
                              conversation_id="c2", started=0.0)
    ident = access.Identity.make("ana@example.org", (), "web")
    asyncio.run(mgr._record_usage(channel, "complete", {}, ident))
    asyncio.run(mgr._record_usage(web_run, "complete", {}, ident))
    assert seen == ["slack", "web"]
    assert router.parse_channel_credential("slack|slack-channel|slack-main") == ("slack", "slack-channel",
                                                                                "slack-main")


def test_unattended_work_owes_no_notice():
    from hubzoid.channels import admission
    from hubzoid.channels.models import ExternalActor, InboundEvent, Origin

    conn = SimpleNamespace(enabled=True, adapter=SimpleNamespace(customer_facing=False, notice_reasons=frozenset()))
    origin = Origin("github", "org", "gh:1", {})
    asked = InboundEvent(semantic_key="k1", kind="comment", origin=origin, actor=ExternalActor("u1"), trigger=True)
    opened = InboundEvent(semantic_key="k2", kind="pull_request", origin=origin, actor=ExternalActor("u1"),
                          trigger=True, data={"unattended": True})
    assert admission._notice_owed(conn, asked, "denied", "queue_full") == {"notice": "queue_full"}
    assert admission._notice_owed(conn, opened, "denied", "queue_full") == {}


def test_an_enterprise_grid_sibling_workspace_is_not_slack_connect(hub_dir, web):
    conn = conn_for(hub_dir)
    body = event(user_team="T0SIBLING")
    body["enterprise_id"] = "E0ORG"
    receive(conn, body)
    assert _rows(hub_dir)[0]["status"] == "accepted"


def test_two_reloads_at_once_start_a_transport_once(hub_dir, monkeypatch):
    c = _conn("slack-main")
    log, _ = _transports(monkeypatch, hub_dir, [c])
    monkeypatch.setattr(router._state, "reload_lock", None)

    async def both():
        await asyncio.gather(router.reload_transports(), router.reload_transports())

    asyncio.run(both())
    assert log == [("start", "slack-main")]


# -- long replies --------------------------------------------------------------------------------

def test_overflow_code_part_keeps_closing_fence():
    """From S0's coverage tests (tests/test_cov_channels_replies.py): a reply
    cut to fit the channel never leaves a code block open."""
    from hubzoid.channels import replies

    part = replies.split("```\n" + "x" * 1000 + "\n```", 240, 1)[0]
    assert part.count("```") % 2 == 0 and part.endswith(replies.OVERFLOW_NOTE) and len(part) <= 240


@settings(max_examples=60, deadline=None)
@given(text=st.lists(st.sampled_from(["a", "word ", "\n", "\n\n", "`", "```", "```\n", "x" * 90]),
                    max_size=120).map("".join).filter(lambda t: t.count("```") % 2 == 0),
       limit=st.integers(240, 800), max_parts=st.integers(1, 4))
def test_every_part_fits_and_closes_its_code_blocks(text, limit, max_parts):
    """Splitting a reply whose code blocks are closed never leaves one open."""
    from hubzoid.channels import replies

    parts = replies.split(text, limit, max_parts)
    assert len(parts) <= max_parts
    assert all(len(p) <= limit and p.count("```") % 2 == 0 for p in parts)


# -- versions of S0's coverage tests (test_cov_channels_router.py) for the new behaviour -------

def test_group_check_waits_when_access_store_is_down(hub_dir, web, monkeypatch):
    """A store error is neither a yes nor a no: None, and the delivery waits to be routed again."""
    from unittest.mock import Mock

    from hubzoid import access

    conn = conn_for(hub_dir)
    app = _linked_app(monkeypatch)
    monkeypatch.setattr(access, "store_for", Mock(side_effect=OSError("down")))
    assert router._groups_ok(conn, "ana@example.org") is None
    receive(conn, event())
    row = store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0]
    assert router._route_one(hub_dir, row, app) == ("waiting", None)
    monkeypatch.setattr(access, "store_for", Mock(side_effect=OSError("still down")))
    again = store.open_deliveries(hub_dir, config.hub_of(hub_dir))[0]
    assert again["id"] == row["id"] and router._route_one(hub_dir, again, app) == ("waiting", None)


def test_transport_reload_replaces_revisions_and_isolates_failures(hub_dir, monkeypatch):
    from unittest.mock import Mock

    state = router._Loop()
    monkeypatch.setattr(router, "_state", state)
    monkeypatch.setattr(router, "_fingerprint", lambda conn: "fp")
    state.hub_dir = hub_dir
    old, current, made = Mock(), Mock(), Mock()
    old.stop.side_effect = RuntimeError("stop failed")
    state.transports = {"old@1@fp": old, "keep@1@fp": current}
    keep_adapter = SimpleNamespace(start=Mock())
    new_adapter = SimpleNamespace(start=Mock(return_value=made))
    bad_adapter = SimpleNamespace(start=Mock(side_effect=RuntimeError("start failed")))
    off_adapter = SimpleNamespace(start=Mock())
    conns = [SimpleNamespace(id="keep", revision=1, enabled=True, adapter=keep_adapter, settings=SimpleNamespace()),
             SimpleNamespace(id="new", revision=2, enabled=True, adapter=new_adapter, settings=SimpleNamespace()),
             SimpleNamespace(id="bad", revision=1, enabled=True, adapter=bad_adapter, settings=SimpleNamespace()),
             SimpleNamespace(id="off", revision=1, enabled=False, adapter=off_adapter, settings=SimpleNamespace())]
    monkeypatch.setattr(router.config_lib, "active_all", lambda _: conns)
    asyncio.run(router.reload_transports())
    assert state.transports == {"keep@1@fp": current, "new@2@fp": made}
    assert old.stop.call_count == 1 and new_adapter.start.call_args.args == (conns[1], hub_dir)
    assert keep_adapter.start.call_count == 0 and off_adapter.start.call_count == 0
    assert "bad@1@fp" in state.backoff                      # tried again later, not every pass
    asyncio.run(router.reload_transports())                 # reloads still happen, a healthy one is kept
    assert state.transports == {"keep@1@fp": current, "new@2@fp": made} and bad_adapter.start.call_count == 1
    monkeypatch.setattr(router.config_lib, "active_all", Mock(side_effect=OSError("unreadable")))
    asyncio.run(router.reload_transports())
    assert state.transports == {"keep@1@fp": current, "new@2@fp": made}
    state.hub_dir = None
    asyncio.run(router.reload_transports())
    assert state.transports["new@2@fp"] is made


def test_doctor_keeps_the_open_mode_warning_when_the_token_check_fails(hub_dir, web, monkeypatch):
    from hubzoid.channels import open_mode

    conn = conn_for(hub_dir)
    open_mode.set_open(conn, enabled=True, actor="owner@example.org")
    monkeypatch.setattr(web, "auth_test", lambda: (_ for _ in ()).throw(
        SlackApiError("refused", {"ok": False, "error": "invalid_auth"})))
    checks = {c.id: c.status for c in conn.adapter.doctor(conn, {})}
    assert checks["slack:auth"] == "fail" and checks["slack:open_mode"] == "warn"
