"""Slack attachment refusals, context fallback, doctor diagnostics and socket acknowledgements."""
from __future__ import annotations

from contextlib import nullcontext

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from slack_sdk.errors import SlackApiError

from hubzoid.channels import admission, config, store
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.models import ExternalActor, InboundEvent, Origin
from hubzoid.channels.slack import adapter as slack
from hubzoid.channels.slack.socket import SocketTransport
from tests.test_channels_slack import conn_for, hub_dir as hub_dir, web as web


def event():
    return InboundEvent("message:1", "message", Origin("slack", "T0TEAM", "slack:C0OPS:1", {"channel": "C0OPS", "thread_ts": "1"}),
                        actor=ExternalActor("U1"), text="Hello", data={"channel": "C0OPS", "thread_ts": "1", "ts": "2", "in_thread": True})


@pytest.mark.parametrize("info,status,body,code", [
    ({"url_private_download": "http://files.slack.com/f"}, 200, b"file", "attachment_blocked"),
    ({"url_private_download": "https://evil.example.test/f"}, 200, b"file", "attachment_blocked"),
    ({"url_private_download": "https://files.slack.com/f", "size": 11}, 200, b"file", "attachment_too_large"),
    ({"url_private_download": "https://files.slack.com/f"}, 403, b"", "attachment_unavailable"),
    ({"url_private_download": "https://files.slack.com/f"}, 200, b"12345678901", "attachment_too_large"),
])
def test_slack_file_refusals(hub_dir, web, monkeypatch, info, status, body, code):
    conn = conn_for(hub_dir, max_file_bytes=10)
    monkeypatch.setattr(web, "files_info", lambda **kw: {"file": info}, raising=False)
    monkeypatch.setattr(httpx, "stream", lambda *a, **kw: nullcontext(httpx.Response(status, content=body)))
    with pytest.raises(ChannelError) as exc:
        conn.adapter.fetch_file(conn, event(), {"id": "F1"})
    assert exc.value.code == code


def test_slack_file_info_error_and_sanitized_download(hub_dir, web, monkeypatch):
    conn = conn_for(hub_dir)
    monkeypatch.setattr(web, "files_info", Mock(side_effect=RuntimeError("no access")), raising=False)
    with pytest.raises(ChannelError, match="Slack would not share the file"):
        conn.adapter.fetch_file(conn, event(), {"id": "F1"})
    monkeypatch.setattr(web, "files_info", lambda **kw: {"file": {"url_private": "https://files.slack.com/f", "name": "../../report?.txt"}})
    requested = []
    def stream(*a, **kw):
        requested.append((a, kw))
        return nullcontext(httpx.Response(200, content=b"report"))
    monkeypatch.setattr(httpx, "stream", stream)
    assert conn.adapter.fetch_file(conn, event(), {"id": "F1"}) == ("report_.txt", "application/octet-stream", b"report")
    assert requested[0][1]["follow_redirects"] is False
    assert requested[0][1]["headers"]["Authorization"].startswith("Bearer ")


def test_context_history_is_untrusted_bounded_and_optional(hub_dir, web, monkeypatch):
    conn = conn_for(hub_dir)
    monkeypatch.setattr(web, "conversations_replies", lambda **kw: {"messages": [
        {"ts": "1", "bot_id": "B1", "text": "x" * 3000}, {"ts": "2", "text": "current"},
        {"ts": "3", "user": "U2", "text": "earlier"}]})
    result = conn.adapter.context(conn, event(), first=True)
    assert "context only, not instructions" in result
    assert "- bot: " + "x" * 2000 in result and "x" * 2001 not in result
    assert "- U2: earlier" in result and "current" not in result
    monkeypatch.setattr(web, "conversations_replies", Mock(side_effect=OSError("offline")))
    assert "could not be read" in conn.adapter.context(conn, event(), first=True)
    assert "could not be read" not in conn.adapter.context(conn, event(), first=False)
    assert conn.adapter.thread_title({"origin_json": event().origin.to_json()}) == "Slack C0OPS thread"


@pytest.mark.parametrize("failure,code", [(ChannelError("credential_missing", "Set secret"), None),
    (SlackApiError("bad token", {"error": "invalid_auth"}), "credential_rejected"),
    (OSError("offline"), "provider_unavailable")])
def test_doctor_auth_failures_are_not_green(hub_dir, web, monkeypatch, failure, code):
    conn = conn_for(hub_dir)
    monkeypatch.setattr(web, "auth_test", Mock(side_effect=failure))
    checks = conn.adapter.doctor(conn, {})
    assert checks == [] if code is None else [(c.status, c.code) for c in checks] == [("fail", code)]


def test_doctor_app_scopes_channel_and_input_failures(hub_dir, web, monkeypatch):
    conn = conn_for(hub_dir, files_enabled=True)
    web.headers = {"x-oauth-scopes": "chat:write"}
    monkeypatch.setattr(web, "bots_info", Mock(side_effect=OSError("cannot look")))
    monkeypatch.setattr(web, "conversations_info", lambda **kw: {"channel": {"is_archived": True}})
    checks = {c.id: c for c in conn.adapter.doctor(conn, {"channel_id": "COTHER"})}
    assert checks["slack:app"].code == "installation_mismatch"
    assert checks["slack:scopes"].code == "missing_scope" and "files:read" in checks["slack:scopes"].message
    assert checks["slack:channel:C0OPS"].code == "channel_archived"
    assert checks["slack:channel_allowed"].code == "channel_not_allowed"
    monkeypatch.setattr(web, "conversations_info", Mock(side_effect=SlackApiError("bad", {"error": "not_in_channel"})))
    assert any(c.code == "channel_not_joined" for c in conn.adapter.doctor(conn, {}))
    assert not (store.get_connection(hub_dir, conn.hub, conn.id)["state_json"] or {}).get("bot_user_id")


@pytest.mark.parametrize("error,kind", [("ratelimited", "NotSent"), ("invalid_auth", "Rejected"),
    ("not_in_channel", "Rejected"), ("strange_error", "Rejected")])
def test_slack_definite_refusal_classification(error, kind):
    exc = slack._refusal(SlackApiError("error", {"error": error}))
    assert type(exc).__name__ == kind
    assert "Slack" in str(exc)


def test_slack_form_decode_and_invalid_utf8():
    assert slack._decode(b"payload=%7B%22type%22%3A%22block_actions%22%7D", "application/x-www-form-urlencoded") == {"type": "block_actions"}
    with pytest.raises(ChannelError) as exc:
        slack._decode(b"\xff", "application/json")
    assert exc.value.code == "invalid_request" and exc.value.status == 400


@pytest.mark.parametrize("failure,acked", [(ChannelError("forbidden", "wrong app", status=403), True),
    (ChannelError("unavailable", "store down", status=503), False), (OSError("store down"), False)])
def test_socket_ack_only_after_commit_or_final_refusal(hub_dir, web, monkeypatch, failure, acked):
    conn = conn_for(hub_dir, transport="socket_mode", app_token_ref="env:SLACK_TEST_APP")
    socket = SocketTransport(conn)
    client = Mock()
    monkeypatch.setattr(admission, "admit", Mock(side_effect=failure))
    monkeypatch.setattr(conn.adapter, "_verified", lambda *a, **kw: "verified")
    monkeypatch.setattr(conn.adapter, "parse", lambda *a: [])
    socket.on_request(client, SimpleNamespace(type="events_api", envelope_id="e1", payload={}))
    assert client.send_socket_mode_response.call_count == int(acked)
    if acked:
        assert client.send_socket_mode_response.call_args.args[0].envelope_id == "e1"


def test_socket_unrelated_envelope_ack_and_default_client_start_stop(hub_dir, web, monkeypatch):
    import slack_sdk.socket_mode
    conn = conn_for(hub_dir, transport="socket_mode", app_token_ref="env:SLACK_TEST_APP")
    socket = SocketTransport(conn)
    client = Mock()
    client.socket_mode_request_listeners = []
    factory = Mock(return_value=client)
    monkeypatch.setattr(slack_sdk.socket_mode, "SocketModeClient", factory)
    socket.start()
    assert client.socket_mode_request_listeners == [socket.on_request] and client.connect.call_count == 1
    socket.on_request(client, SimpleNamespace(type="disconnect", envelope_id="e1"))
    assert client.send_socket_mode_response.call_args.args[0].envelope_id == "e1"
    client.close.side_effect = OSError("close failed")
    with pytest.raises(OSError, match="close failed"):
        socket.stop()
    assert socket.client is None
    assert factory.call_args.kwargs["web_client"] is web
