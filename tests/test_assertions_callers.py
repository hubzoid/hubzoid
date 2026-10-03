"""Every internal caller that sends identity headers to a bridge signs them in
the web app mode (Slack adapter, inbound dispatch and harness), Slack senders
map to Hubzoid accounts there, and the bridge applies the rule end to end."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from hubzoid import _request_ctx, access, assertions, secretbox
from hubzoid.access import audit as auditlib


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_SECRET_KEY",
              "HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(k, raising=False)
    from hubzoid import migrations

    access._stores.clear()
    migrations._done.clear()
    auditlib._IMPORTED.clear()
    secretbox.reset_cache()
    yield
    access._stores.clear()
    secretbox.reset_cache()


@pytest.fixture
def hub(tmp_path):
    hub = tmp_path / "sales"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: sales\ndescription: d\n---\nbody")
    return hub


def _lower(headers) -> dict:
    return {k.lower(): v for k, v in dict(headers).items()}


def _add_account(hub, email, status="active"):
    gs = access.store_for(hub)
    with gs.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO hz_users (id, email, name, role, status, source, created_at, updated_at) "
            "VALUES (:i, :e, :n, 'user', :s, 'admin', :t, :t)"),
            {"i": "u-" + email, "e": email, "n": email.split("@")[0], "s": status, "t": time.time()})


class _SSE:
    status_code = 200

    def iter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield "data: [DONE]"

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def test_slack_stream_reply_signs_the_identity(hub):
    from hubzoid.slack.adapter import stream_reply

    fake = MagicMock()
    fake.stream.return_value = _SSE()
    stream_reply(bridge_url="http://x/v1", api_key="k", model="m",
                 messages=[{"role": "user", "content": "hi"}], on_delta=lambda _d: None,
                 user_email="priya@example.org", surface="slack-dm", http_client=fake, hub_dir=hub)
    headers = fake.stream.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer k"
    vouched = assertions.vouched(hub, _lower(headers))
    assert (vouched.email, vouched.surface) == ("priya@example.org", "slack-dm")


def test_slack_stream_reply_legacy_headers_are_unchanged(hub, monkeypatch):
    from hubzoid.slack.adapter import stream_reply

    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    fake = MagicMock()
    fake.stream.return_value = _SSE()
    stream_reply(bridge_url="http://x/v1", api_key="k", model="m", messages=[],
                 on_delta=lambda _d: None, user_email="Priya@example.org", surface="slack-dm",
                 http_client=fake, hub_dir=hub)
    assert fake.stream.call_args.kwargs["headers"] == {
        "Authorization": "Bearer k", "X-Hubzoid-Surface": "slack-dm",
        "X-OpenWebUI-User-Email": "Priya@example.org"}


def test_slack_senders_map_to_hubzoid_accounts(hub, monkeypatch):
    from hubzoid.channel_identity import account_email

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    _add_account(hub, "priya@example.org")
    _add_account(hub, "pending@example.org", status="pending")
    assert account_email(hub, "Priya@Example.org") == "priya@example.org"
    assert account_email(hub, "pending@example.org") is None      # not approved yet
    assert account_email(hub, "stranger@example.org") is None     # no account
    assert account_email(hub, None) is None
    access.store_for(hub).suspend("priya@example.org", actor="t")
    assert account_email(hub, "priya@example.org") is None        # blocked
    # Sign-in off: no accounts to map to, the verified email is used as it is.
    monkeypatch.setenv("HUBZOID_AUTH", "false")
    assert account_email(hub, "Stranger@example.org") == "stranger@example.org"
    # Legacy: unchanged (Open WebUI groups are looked up by the bridge).
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    assert account_email(hub, "Pending@example.org") == "Pending@example.org"


def test_slack_account_lookup_fails_closed(hub, monkeypatch):
    from hubzoid.channel_identity import account_email

    def boom(_hub):
        raise RuntimeError("no database")

    monkeypatch.setattr(access, "store_for", boom)
    assert account_email(hub, "priya@example.org") is None


def test_slack_adapter_sends_the_account_it_mapped(hub, monkeypatch):
    """The adapter's DM path: the Slack profile email becomes the Hubzoid
    account's email (or nobody) and is signed for this hub."""
    from hubzoid.slack import adapter

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    _add_account(hub, "priya@example.org")
    sent = []
    monkeypatch.setattr(adapter, "stream_reply", lambda **kw: sent.append(kw))
    monkeypatch.setattr(adapter, "_lookup_email",
                        lambda _c, uid: {"U1": "Priya@example.org", "U2": "ghost@example.org"}[uid])
    app = adapter.build_app(hub_dir=hub, bridge_url="http://x/v1", api_key="k", model_label="m",
                            bot_token="xoxb-test", suggestions=[], verify_token=False,
                            identity_mapping=True)
    # Drive the DM handler directly through the registered function.
    im = [li for li in app._listeners if getattr(li.ack_function, "__name__", "") == "_on_im"]
    assert im, "DM listener not registered"
    client = MagicMock()
    client.conversations_replies.return_value = {"messages": [{"user": "U1", "text": "hi", "ts": "1"}]}
    client.chat_postMessage.return_value = {"ts": "2"}
    ctx = MagicMock(bot_id="B", bot_user_id="UB")
    for uid in ("U1", "U2"):
        im[0].ack_function(event={"channel": "D1", "ts": "1", "user": uid}, client=client,
                           context=ctx, say=MagicMock())
    assert [kw["user_email"] for kw in sent] == ["priya@example.org", None]
    assert all(kw["hub_dir"] == hub for kw in sent)


# ---------------------------------------------------------------------------
# inbound (WhatsApp / Telegram)
# ---------------------------------------------------------------------------
def test_inbound_dispatch_signs_the_roster_identity(hub):
    from hubzoid.inbound.dispatch import dispatch

    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content="data: [DONE]\n\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch(bridge_url="http://x/v1", api_key="k", model="m", messages=[], surface="whatsapp",
             user_email="ravi@example.org", groups=["Coordinator"], http_client=client, hub_dir=hub)
    vouched = assertions.vouched(hub, captured["headers"])
    assert (vouched.email, vouched.groups, vouched.surface) == ("ravi@example.org", ("coordinator",), "whatsapp")


def test_inbound_harness_signs_through_the_real_dispatch(hub, monkeypatch):
    from hubzoid.inbound import dispatch as dispatch_mod
    from hubzoid.inbound.harness import build_app
    from hubzoid.inbound.env import WhatsAppConfig

    captured = []
    real_client = httpx.Client

    def handler(request):
        captured.append(dict(request.headers))
        return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(dispatch_mod.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="TKN", phone_number_id="PNID",
                        send_text=lambda **kw: {}, mark_read=lambda **kw: {})
    roster = {"919800000001": {"email": "ravi@example.org", "groups": ["coordinator"]}}
    app = build_app(hub_dir=hub, bridge_url="http://bridge/v1", api_key="k", model="m",
                    resolver=lambda _s, handle: roster.get(handle), whatsapp=wa)
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "contacts": [{"wa_id": "919800000001"}],
        "messages": [{"from": "919800000001", "id": "wamid.A1", "type": "text", "text": {"body": "hi"}}]}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"SEC", raw, hashlib.sha256).hexdigest()
    r = TestClient(app).post("/webhooks/whatsapp", content=raw,
                             headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    [headers] = captured
    vouched = assertions.vouched(hub, headers)
    assert vouched.email == "ravi@example.org" and vouched.groups == ("coordinator",)


# ---------------------------------------------------------------------------
# the bridge, end to end
# ---------------------------------------------------------------------------
@pytest.fixture
def bridge(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "k-bridge")
    from hubzoid.server import build_app

    return TestClient(build_app())


async def _answer(self, prompt):
    _request_ctx.record_usage({"input_tokens": 1, "output_tokens": 1, "model": "m", "status": "ok"})
    return "ok"


def _usage(hub):
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    with eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT surface, subject FROM hz_usage"))]


def _chat(bridge, headers):
    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=_answer):
        return bridge.post("/v1/chat/completions",
                           headers={"Authorization": "Bearer k-bridge", **headers},
                           json={"model": "sales", "chat_id": "c-1",
                                 "messages": [{"role": "user", "content": "hello"}]})


def test_bridge_counts_only_signed_identities(bridge, hub):
    from tests.access_helpers import allow

    allow(hub, "ann@example.org", "boss@example.org")
    signed = assertions.identity_headers(hub, surface="whatsapp", email="ann@example.org")
    assert _chat(bridge, signed).status_code == 200
    # Unsigned headers are nobody, and nobody may use the agent.
    forged = {"X-OpenWebUI-User-Email": "boss@example.org", "X-Hubzoid-Surface": "slack-dm"}
    assert _chat(bridge, forged).status_code == 403
    assert _usage(hub) == [{"surface": "whatsapp", "subject": "ann@example.org"}]


def test_bridge_managed_hub_refuses_an_unsigned_identity(bridge, hub):
    gs = access.store_for(hub)
    gs.bootstrap(["boss@example.org"])
    forged = {"X-OpenWebUI-User-Email": "boss@example.org"}
    r = _chat(bridge, forged)
    assert r.status_code == 403 and "requires sign-in" in r.text
    ok = _chat(bridge, assertions.identity_headers(hub, surface="whatsapp", email="boss@example.org"))
    assert ok.status_code == 403  # signed, but the owner holds no use_hub here yet
    gs.grant("boss@example.org", "sales", "use_hub", actor="t")
    ok = _chat(bridge, assertions.identity_headers(hub, surface="whatsapp", email="boss@example.org"))
    assert ok.status_code == 200
