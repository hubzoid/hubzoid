"""The Slack channel adapter with a fake Slack Web API: signatures, events,
replies, recovery markers, Socket Mode acknowledgement and the manifest."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from slack_sdk.signature import SignatureVerifier

from hubzoid.channels import admission, config, replies, store
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.slack import adapter as slack

SIGNING = "slack-signing-value"   # gitleaks:allow (a test value)
BOT = "xoxb-test-value"           # gitleaks:allow (a test value)


class FakeWeb:
    """The Slack Web API calls the adapter makes, recorded."""

    def __init__(self):
        self.posts: list[dict] = []
        self.reactions: list[dict] = []
        self.fail_post = None
        self.team = "T0TEAM"
        self.headers = {"x-oauth-scopes": ",".join(slack.BASE_SCOPES + ["files:read"])}

    def chat_postMessage(self, **kw):
        if self.fail_post is not None:
            exc, self.fail_post = self.fail_post, None
            raise exc
        ts = f"{1700000000 + len(self.posts)}.000{len(self.posts) + 1:03d}"
        self.posts.append({**kw, "ts": ts})
        return {"ok": True, "ts": ts, "channel": kw["channel"]}

    def reactions_add(self, **kw):
        self.reactions.append(kw)
        return {"ok": True}

    def conversations_replies(self, **kw):
        msgs = [{"ts": p["ts"], "text": p["text"], "bot_id": "B1", "metadata": p.get("metadata")}
                for p in self.posts if p["channel"] == kw["channel"] and p.get("thread_ts") == kw["ts"]]
        return {"ok": True, "messages": msgs, "response_metadata": {}}

    def auth_test(self):
        return SimpleNamespace(get=lambda k, d=None: {"team_id": self.team, "user_id": "UBOT",
                                                      "bot_id": "B1"}.get(k, d), headers=self.headers)

    def bots_info(self, bot):
        return {"ok": True, "bot": {"app_id": "A0APP"}}

    shared = False

    def conversations_info(self, channel):
        return {"ok": True, "channel": {"id": channel, "name": "ops", "is_member": True, "is_ext_shared": self.shared}}


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(slack, "client", lambda conn, writes=False: fake)
    return fake


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_PUBLIC_URL", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SLACK_TEST_SIGNING", SIGNING)
    monkeypatch.setenv("SLACK_TEST_BOT", BOT)
    monkeypatch.setenv("SLACK_TEST_APP", "xapp-test-value")   # gitleaks:allow
    root = tmp_path / "ops"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Ops\ndescription: The ops agent.\n---\nOps.\n")
    return root


def conn_for(hub_dir, **pc):
    provider_config = {"transport": "http", "installation_team_id": "T0TEAM", "app_id": "A0APP",
                       "bot_token_ref": "env:SLACK_TEST_BOT", "signing_secret_ref": "env:SLACK_TEST_SIGNING",
                       "allowed_channel_ids": ["C0OPS"], **pc}
    v = config.load_text(hub_dir, config.render({"version": 1, "id": "slack-main", "provider": "slack",
                                                 "enabled": True, "agent": "main", "allowed_groups": ["ops"],
                                                 "provider_config": provider_config}))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    return config.active(hub_dir, "slack-main")


def signed(body: dict, secret=SIGNING, age=0):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) - age)
    sig = SignatureVerifier(secret).generate_signature(timestamp=ts, body=raw)
    return raw, {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}


def event(etype="app_mention", channel="C0OPS", ts="1700000000.000100", thread_ts=None, user="U0ANA",
          text="<@UBOT> check stock for SKU 42", event_id="Ev1", **extra):
    ev = {"type": etype, "channel": channel, "ts": ts, "user": user, "text": text, **extra}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return {"type": "event_callback", "api_app_id": "A0APP", "team_id": "T0TEAM", "event_id": event_id,
            "event": ev}


def receive(conn, body, **kw):
    raw, headers = signed(body, **kw)
    return admission.receive(conn.hub_dir, conn.id, raw, headers, {})


# -- configuration ---------------------------------------------------------------

def test_transport_needs_its_secret(hub_dir):
    bad = config.load_text(hub_dir, config.render({"version": 1, "id": "s", "provider": "slack", "agent": "main",
                                                   "provider_config": {"transport": "socket_mode",
                                                                       "installation_team_id": "T1", "app_id": "A1",
                                                                       "bot_token_ref": "env:X"}}))
    assert not bad.valid and "app_token_ref" in bad.errors[0]["message"]
    names = config.load_text(hub_dir, config.render({"version": 1, "id": "s", "provider": "slack", "agent": "main",
                                                     "provider_config": {"transport": "http",
                                                                         "installation_team_id": "T1", "app_id": "A1",
                                                                         "bot_token_ref": "env:X",
                                                                         "signing_secret_ref": "env:Y",
                                                                         "allowed_channel_ids": ["#general"]}}))
    assert not names.valid and "Names are not accepted" in names.errors[0]["message"]
    shared = config.load_text(hub_dir, config.render({"version": 1, "id": "s", "provider": "slack", "agent": "main",
                                                      "provider_config": {"transport": "http",
                                                                          "installation_team_id": "T1",
                                                                          "app_id": "A1", "bot_token_ref": "env:X",
                                                                          "signing_secret_ref": "env:Y",
                                                                          "allow_shared_channels": True}}))
    assert not shared.valid    # Slack Connect stays denied: the setting does not exist


# -- verification ------------------------------------------------------------------

def test_http_signature_raw_and_stale(hub_dir):
    conn = conn_for(hub_dir)
    out = receive(conn, event())
    assert out.status == "accepted"
    with pytest.raises(ChannelError) as exc:
        receive(conn, event(event_id="Ev2"), secret="wrong")
    assert exc.value.status == 401
    with pytest.raises(ChannelError):
        receive(conn, event(event_id="Ev3"), age=60 * 10)


def test_url_verification_challenge_even_when_paused(hub_dir):
    from hubzoid.channels.models import Challenge

    conn = conn_for(hub_dir)
    config.set_enabled(hub_dir, conn.id, False, expected_revision=1, actor="t")
    out = receive(conn, {"type": "url_verification", "challenge": "abc"})
    assert isinstance(out, Challenge) and out.body == {"challenge": "abc"}


def test_app_and_team_binding(hub_dir):
    conn = conn_for(hub_dir)
    with pytest.raises(ChannelError) as exc:
        receive(conn, {**event(), "api_app_id": "AOTHER"})
    assert exc.value.code == "installation_mismatch"
    with pytest.raises(ChannelError):
        receive(conn, {**event(), "team_id": "TOTHER"})


# -- events --------------------------------------------------------------------------

def _first(conn, body):
    out = receive(conn, body)
    row = store.get_delivery(conn.hub_dir, out.events[0]["id"])
    return row, admission.event_of(conn.hub_dir, row) if row.get("event_enc") else None


def test_mention_in_allowed_channel_starts_a_thread(hub_dir):
    conn = conn_for(hub_dir)
    row, ev = _first(conn, event())
    assert row["status"] == "accepted" and ev.trigger and ev.text == "check stock for SKU 42"
    assert ev.origin.thread_key == "slack:C0OPS:1700000000.000100"
    assert isinstance(ev.data["ts"], str) and ev.data["ts"] == "1700000000.000100"


def test_mention_inside_a_thread_keeps_its_root(hub_dir):
    conn = conn_for(hub_dir)
    _, ev = _first(conn, event(ts="1700000001.000200", thread_ts="1700000000.000100"))
    assert ev.origin.coordinates == {"channel": "C0OPS", "thread_ts": "1700000000.000100"}
    assert ev.data["in_thread"]


@pytest.mark.parametrize("body,reason", [
    (event(channel="C0OTHER"), "channel_not_allowed"),
    (event(subtype="message_changed", etype="message"), "subtype_message_changed"),
    ({**event(), "is_ext_shared_channel": True}, "shared_channel_denied"),
])
def test_unsupported_events_are_recorded_and_ignored(hub_dir, body, reason):
    conn = conn_for(hub_dir)
    row, _ = _first(conn, body)
    assert row["status"] == "ignored" and row["reason"] == reason


def test_bot_echo_ignored(hub_dir):
    conn = conn_for(hub_dir)
    row, _ = _first(conn, event(bot_id="B1"))
    assert row["reason"] == "bot_author"


def test_direct_messages(hub_dir):
    conn = conn_for(hub_dir)
    row, ev = _first(conn, event(etype="message", channel="D0ANA", text="hello", channel_type="im"))
    assert row["status"] == "accepted" and ev.trigger
    off = conn_for(hub_dir.parent / "x") if False else None
    del off


def test_direct_messages_off(hub_dir):
    conn = conn_for(hub_dir, allow_direct_messages=False)
    row, _ = _first(conn, event(etype="message", channel="D0ANA", text="hello", channel_type="im"))
    assert row["reason"] == "direct_messages_off"


def test_mention_message_overlap_once(hub_dir):
    conn = conn_for(hub_dir)
    receive(conn, event(etype="message", event_id="EvA", text="<@UBOT> hi", channel_type="channel"))
    receive(conn, event(etype="app_mention", event_id="EvB", text="<@UBOT> hi"))
    assert len(store.open_deliveries(hub_dir, "ops")) == 1


def test_unseen_retry_is_admitted(hub_dir):
    """Slack's retry header on a delivery we never saw does not drop it."""
    conn = conn_for(hub_dir)
    raw, headers = signed(event())
    headers["x-slack-retry-num"] = "1"
    out = admission.receive(hub_dir, conn.id, raw, headers, {})
    assert out.status == "accepted"


# -- replies ------------------------------------------------------------------------

def test_mentions_escaped():
    a = slack.SlackAdapter()
    out = a.render("Ping <!channel> and <@U123>, see [docs](https://x.example) & **bold**")
    assert "<!channel>" not in out and "<@U123>" not in out and "&lt;!channel&gt;" in out
    assert "<https://x.example|docs>" in out and "*bold*" in out


def test_reply_goes_to_the_stored_thread_with_a_marker(hub_dir, web):
    conn = conn_for(hub_dir)
    out = receive(conn, event())
    thread = store.get_thread(hub_dir, out.events[0]["thread_id"])
    res = replies.post(conn, thread, kind="result", text="42 in stock.", key="r1")
    assert res[0]["state"] == "succeeded"
    post = web.posts[0]
    assert post["channel"] == "C0OPS" and post["thread_ts"] == "1700000000.000100"
    assert post["reply_broadcast"] is False and post["unfurl_links"] is False
    assert post["metadata"]["event_payload"]["marker"].startswith("hz-")


def test_uncertain_post_settled_by_marker_never_resent(hub_dir, web):
    conn = conn_for(hub_dir)
    out = receive(conn, event())
    thread = store.get_thread(hub_dir, out.events[0]["thread_id"])
    web.fail_post = TimeoutError("read timeout")
    res = replies.post(conn, thread, kind="result", text="Answer.", key="r1")
    assert res[0]["state"] == "ambiguous" and web.posts == []
    check = replies.resolve(conn, res[0]["operation_id"], decision="check", actor="admin@example.org")
    assert check["found"] is False and check["state"] == "ambiguous"


def test_slack_refusal_is_not_ambiguous(hub_dir, web):
    from slack_sdk.errors import SlackApiError

    conn = conn_for(hub_dir)
    out = receive(conn, event())
    thread = store.get_thread(hub_dir, out.events[0]["thread_id"])
    web.fail_post = SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"})
    res = replies.post(conn, thread, kind="result", text="Answer.", key="r1")
    assert res[0]["state"] == "failed" and res[0]["code"] == "operation_rejected"


def test_customer_reply_draft_rejected(hub_dir):
    """A Slack post has no real draft: draft mode cannot apply to it."""
    from hubzoid.work_items import config as wconfig

    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes:\n  customer_reply.send: draft\n")
    cfg = wconfig.load(hub_dir)
    assert wconfig.mode_for(cfg, ["customer_reply.send"], "slack-main") == "draft"
    assert getattr(slack.SlackAdapter, "draft", None) is None   # so operations.run refuses draft_unsupported


# -- socket mode -----------------------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.acks = []
        self.socket_mode_request_listeners = []

    def send_socket_mode_response(self, response):
        self.acks.append(response.envelope_id)

    def connect(self):
        pass

    def close(self):
        pass


def test_socket_commit_precedes_ack(hub_dir, monkeypatch):
    from hubzoid.channels.slack.socket import SocketTransport

    conn = conn_for(hub_dir, transport="socket_mode", signing_secret_ref=None, app_token_ref="env:SLACK_TEST_APP")
    sock = FakeSocket()
    t = SocketTransport(conn, client_factory=lambda token, web: sock)
    monkeypatch.setattr(slack, "client", lambda conn, writes=False: FakeWeb())
    t.start()
    req = SimpleNamespace(type="events_api", envelope_id="env-1", payload=event())
    t.on_request(sock, req)
    assert sock.acks == ["env-1"] and len(store.open_deliveries(hub_dir, "ops")) == 1
    # Storage down: no acknowledgement, so Slack redelivers.
    monkeypatch.setattr(admission, "admit", lambda *a, **k: (_ for _ in ()).throw(
        ChannelError("unavailable", "down", status=503)))
    t.on_request(sock, SimpleNamespace(type="events_api", envelope_id="env-2", payload=event(event_id="Ev9")))
    assert sock.acks == ["env-1"]


# -- setup ---------------------------------------------------------------------------------

def test_manifest_scopes_and_transport(hub_dir, monkeypatch):
    from hubzoid.channels.slack import manifest

    conn = conn_for(hub_dir)
    m = manifest.build(conn)
    scopes = m["oauth_config"]["scopes"]["bot"]
    assert "chat:write.public" not in scopes and "users:read.email" not in scopes and "files:read" in scopes
    assert m["settings"]["socket_mode_enabled"] is False
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.test")
    m = manifest.build(conn)
    assert m["settings"]["event_subscriptions"]["request_url"] == \
        "https://hub.example.test/api/hubs/ops/channels/slack-main/webhook"
    assert "message.channels" in m["settings"]["event_subscriptions"]["bot_events"]


def test_doctor_checks_workspace_app_and_channel(hub_dir, web):
    from hubzoid.channels import doctor

    conn = conn_for(hub_dir)
    checks = {c["id"]: c for c in doctor.run(conn)["checks"]}
    assert checks["slack:workspace"]["status"] == "ok" and checks["slack:app"]["status"] == "ok"
    assert checks["slack:channel:C0OPS"]["status"] == "ok" and checks["slack:scopes"]["status"] == "ok"
    web.team = "TOTHER"
    checks = {c["id"]: c for c in doctor.run(conn)["checks"]}
    assert checks["slack:workspace"]["code"] == "installation_mismatch"


def test_legacy_listener_refuses_when_a_channel_serves_slack(hub_dir):
    from hubzoid.slack.adapter import _channel_conflict

    assert _channel_conflict(hub_dir) is None
    conn_for(hub_dir)
    assert _channel_conflict(hub_dir) == "slack-main"


def test_reply_refused_when_the_channel_became_shared(hub_dir, web):
    from hubzoid.work_items.errors import Rejected

    conn = conn_for(hub_dir)
    web.shared = True
    with pytest.raises(Rejected) as exc:
        conn.adapter.reply(conn, slack.Origin("slack", "T0TEAM", "slack:C0OPS:1.0", {"channel": "C0OPS",
                                                                                     "thread_ts": "1.0"}),
                           SimpleNamespace(text="the answer", marker="m1"))
    assert "shared outside" in str(exc.value) and web.posts == []


def test_import_legacy_never_overwrites(hub_dir, monkeypatch):
    from typer.testing import CliRunner

    from hubzoid.cli import app

    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT)
    (hub_dir / "channels").mkdir()
    (hub_dir / "channels" / "slack-main.yaml").write_text("# mine\n")
    out = CliRunner().invoke(app, ["channels", "slack", "import-legacy", str(hub_dir), "--json"])
    assert out.exit_code != 0 and "already_exists" in out.output
    assert (hub_dir / "channels" / "slack-main.yaml").read_text() == "# mine\n"
