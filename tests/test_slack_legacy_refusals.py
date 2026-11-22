"""The 1.x Slack app's three handlers when the bridge call fails: a generic
notice with a reference in place of the "…" placeholder, never the exception,
a URL, a host or the bridge's detail. Versions of S0's coverage tests
(``test_cov_channels_slack_handlers.py``) matching hubzoid.bridge_refusal."""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from hubzoid import bridge_refusal
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
                            bot_token="xoxb-fake", suggestions=[], verify_token=False, identity_mapping=True)
    client = Mock()
    client.conversations_replies.return_value = {"messages": [{"user": "U1", "ts": "1", "text": "Hello"}]}
    client.users_info.return_value = {"user": {"profile": {"email": "ana@example.org"}}}
    client.chat_postMessage.return_value = {"ts": "reply"}
    context = SimpleNamespace(bot_user_id="BOT", bot_id="B1")
    say = Mock(return_value={"ts": "reply"})
    return app, client, context, say


def _run(app, client, context, say, kind):
    event = {"channel": "D1", "ts": "1", "thread_ts": "1", "user": "U1"}
    if kind == "assistant":
        app.assistant_handlers["message"](client=client, payload=event, context=context, set_status=Mock(), say=say)
    else:
        app.handlers["app_mention" if kind == "mention" else "im"](event, client, context, say)


@pytest.mark.parametrize("kind", ["mention", "im", "assistant"])
def test_a_failed_bridge_call_replaces_the_placeholder_with_a_generic_notice(slack, monkeypatch, kind):
    app, client, context, say = slack
    monkeypatch.setattr(adapter, "stream_reply", Mock(side_effect=RuntimeError(
        "bridge down at http://127.0.0.1:8000/v1/chat/completions for ana@example.org")))
    _run(app, client, context, say, kind)
    text = client.chat_update.call_args.kwargs["text"]
    assert client.chat_update.call_args.kwargs["ts"] == "reply"            # the "…" placeholder
    assert re.fullmatch(r"I could not answer right now\. Try again shortly\. \(ref [0-9a-f]{8}\)", text)
    for leak in ("RuntimeError", "bridge down", "http", "127.0.0.1", "ana@example.org"):
        assert leak not in text


@pytest.mark.parametrize("kind", ["mention", "im", "assistant"])
def test_a_refusal_after_the_answer_never_replaces_it(slack, monkeypatch, kind):
    app, client, context, say = slack
    monkeypatch.setattr(adapter, "stream_reply", lambda **kw: kw["on_delta"]("The answer"))
    monkeypatch.setattr(adapter.time, "time", lambda: 0.0)
    from hubzoid.hitl import channels

    monkeypatch.setattr(channels, "pending_in_chat", Mock(side_effect=RuntimeError("store down")))
    _run(app, client, context, say, kind)
    assert client.chat_update.call_args.kwargs["text"] == "The answer"
    posted = [c.kwargs.get("text") or (c.args[0] if c.args else "") for c in
              client.chat_postMessage.call_args_list + say.call_args_list]
    assert any(t.startswith("I could not answer right now.") for t in posted)


def test_a_403_says_ask_for_access_without_the_bridge_detail(slack, monkeypatch):
    app, client, context, say = slack
    request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    refusal = httpx.HTTPStatusError("403", request=request, response=httpx.Response(
        403, json={"detail": "You do not have access. Signed in as ana@example.org."}, request=request))
    monkeypatch.setattr(adapter, "stream_reply", Mock(side_effect=refusal))
    _run(app, client, context, say, "mention")
    assert client.chat_update.call_args.kwargs["text"] == bridge_refusal.NO_ACCESS
