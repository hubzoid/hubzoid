"""Slack event handlers preserve sender identity, thread scope and human decision results."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hubzoid.slack import adapter


class App:
    def __init__(self, **kwargs):
        self.handlers = {}
        self.assistant_handlers = None

    def action(self, pattern):
        return lambda fn: self.handlers.setdefault("action", fn)

    def event(self, spec):
        key = spec if isinstance(spec, str) else "im"
        return lambda fn: self.handlers.setdefault(key, fn)

    def assistant(self, instance):
        self.assistant_handlers = instance.handlers


class Assistant:
    def __init__(self):
        self.handlers = {}

    def thread_started(self, fn):
        self.handlers["started"] = fn
        return fn

    def user_message(self, fn):
        self.handlers["message"] = fn
        return fn


@pytest.fixture
def slack(tmp_path, monkeypatch):
    from hubzoid import channel_identity
    from hubzoid.hitl import channels
    monkeypatch.setattr(adapter, "App", App)
    monkeypatch.setattr(adapter, "Assistant", Assistant)
    monkeypatch.setattr(adapter, "download_message_files", lambda **kw: {})
    monkeypatch.setattr(channel_identity, "account_email", lambda root, email: email)
    monkeypatch.setattr(channels, "pending_in_chat", lambda *a, **kw: [])
    app = adapter.build_app(hub_dir=tmp_path, bridge_url="http://bridge/v1", api_key="test", model_label="desk",
                            bot_token="xoxb-fake", suggestions=["one", "two", "three", "four", "five"],
                            verify_token=False, identity_mapping=True)
    client = Mock()
    client.conversations_replies.return_value = {"messages": [{"user": "U1", "ts": "1", "text": "Hello"}]}
    client.users_info.return_value = {"user": {"profile": {"email": "ana@example.org"}}}
    client.chat_postMessage.return_value = {"ts": "reply"}
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    context = SimpleNamespace(bot_user_id="BOT", bot_id="B1")
    say = Mock(return_value={"ts": "reply"})
    return app, client, context, say


@pytest.mark.parametrize("kind,channel,surface", [("mention", "C1", "slack-channel"),
    ("im", "D1", "slack-dm"), ("assistant", "D1", "slack-dm")])
@pytest.mark.parametrize("answer", ["Done", ""])
def test_slack_handlers_stream_to_stored_thread_and_sender(slack, monkeypatch, kind, channel, surface, answer):
    app, client, context, say = slack
    calls = []
    def stream(**kw):
        calls.append(kw)
        kw["on_delta"](answer)
    monkeypatch.setattr(adapter, "stream_reply", stream)
    event = {"channel": channel, "ts": "1", "thread_ts": "1", "user": "U1", "text": "Hello"}
    if kind == "assistant":
        status = Mock()
        app.assistant_handlers["message"](client=client, payload=event, context=context, set_status=status, say=say)
        status.assert_called_once_with("Thinking...")
    else:
        app.handlers["app_mention" if kind == "mention" else "im"](event, client, context, say)
    assert len(calls) == 1 and calls[0]["surface"] == surface
    assert calls[0]["user_email"] == "ana@example.org"
    assert calls[0]["chat_id"] == f"slack-{channel}-1"
    assert calls[0]["messages"][-1]["content"] == "Hello"
    assert client.chat_update.call_args.kwargs == {"channel": channel, "ts": "reply",
                                                   "text": answer or "(no response)"}


@pytest.mark.parametrize("event", [{"subtype": "bot_message"}, {"bot_id": "B1"},
                                  {"assistant_thread": {} , "subtype": "message_changed"},
                                  {"assistant_thread": {"id": "a1"}}, {"subtype": "message_changed"}])
def test_im_echo_and_assistant_events_do_not_start_another_turn(slack, event):
    app, client, context, say = slack
    app.handlers["im"](event, client, context, say)
    assert client.conversations_replies.call_count == 0 and say.call_count == 0


def test_assistant_greeting_limits_suggestions(slack):
    app, _, _, say = slack
    suggestions = Mock()
    app.assistant_handlers["started"](say=say, set_suggested_prompts=suggestions)
    assert "Ask me anything" in say.call_args.args[0]
    assert suggestions.call_args.kwargs["prompts"] == [
        {"title": word, "message": word} for word in ("one", "two", "three", "four")]


@pytest.mark.parametrize("private", [True, False])
def test_requests_are_offered_privately_for_shared_threads(slack, monkeypatch, private):
    from hubzoid.hitl import channels
    app, client, context, say = slack
    monkeypatch.setattr(adapter, "stream_reply", lambda **kw: kw["on_delta"]("Waiting"))
    monkeypatch.setattr(channels, "pending_in_chat", lambda *a, **kw: [{"id": "r1", "title": "Approve action"}])
    monkeypatch.setattr(channels, "slack_blocks", lambda req: [{"type": "section", "text": "Action"}])
    channel = "D1" if private else "C1"
    event = {"channel": channel, "ts": "1", "user": "U1"}
    app.handlers["im" if private else "app_mention"](event, client, context, say)
    offers = [c.kwargs for c in client.chat_postMessage.call_args_list if "blocks" in c.kwargs]
    assert offers == [{"channel": "D1", **({"thread_ts": "1"} if private else {}),
                       "blocks": [{"type": "section", "text": "Action"}], "text": "Approve action"}]
    assert client.conversations_open.call_count == int(not private)


def test_human_decision_failure_is_ephemeral_and_does_not_stream(slack, monkeypatch):
    from hubzoid.hitl import channels, service
    app, client, context, _ = slack
    monkeypatch.setattr(channels, "slack_decide", Mock(side_effect=service.RequestError(403, "forbidden", "Not yours")))
    ack = Mock()
    app.handlers["action"](ack, {"user": {"id": "U1"}, "actions": [{"action_id": "hz_hitl_yes", "value": "r1"}],
                                "container": {"channel_id": "C1", "message_ts": "1"}}, client, context)
    assert ack.call_count == 1
    client.chat_postEphemeral.assert_called_once_with(channel="C1", user="U1", text="Not yours")
    assert client.chat_update.call_count == 0


@pytest.mark.parametrize("continue_once", [True, False])
def test_human_decision_updates_controls_and_continues_at_most_once(slack, monkeypatch, continue_once):
    from hubzoid.hitl import channels, service
    app, client, context, _ = slack
    request = {"id": "r1", "title": "Action", "conversation_id": "slack-C1-1", "requester_email": "ana@example.org"}
    monkeypatch.setattr(channels, "slack_decide", lambda *a, **kw: request)
    monkeypatch.setattr(service, "visible_outcome", lambda _: "Approved")
    monkeypatch.setattr(service, "mark_continued", lambda *a: continue_once)
    monkeypatch.setattr(service, "outcome_text", lambda _: "Action approved")
    stream = Mock(side_effect=lambda **kw: kw["on_delta"]("Continued"))
    monkeypatch.setattr(adapter, "stream_reply", stream)
    ack = Mock()
    app.handlers["action"](ack, {"user": {"id": "U1"}, "container": {"channel_id": "D1", "message_ts": "2"}}, client, context)
    assert client.chat_update.call_args_list[0].kwargs == {"channel": "D1", "ts": "2", "text": "Approved: Action", "blocks": []}
    assert stream.call_count == int(continue_once)
    if continue_once:
        assert stream.call_args.kwargs["messages"][-1] == {"role": "user", "content": "Action approved"}
        assert stream.call_args.kwargs["surface"] == "slack-channel"


def test_throttled_writer_failure_keeps_buffer_for_final_flush(caplog):
    writer = Mock(side_effect=[OSError("update failed"), None])
    throttled = adapter._ThrottledWriter(writer, interval=0)
    throttled.feed("hello")
    assert throttled.done() == "hello" and writer.call_count == 2
    assert "mid-stream" in caplog.text
    failing = adapter._ThrottledWriter(Mock(side_effect=OSError("failed")))
    failing._buf = ["kept"]
    assert failing.done() == "kept" and "final flush" in caplog.text
