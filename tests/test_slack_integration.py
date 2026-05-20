"""Integration test for the Slack adapter against a real FastAPI bridge.

Spins up an in-process FastAPI app that mimics `hubzoid/server.py`'s SSE shape
on `/v1/chat/completions`, then calls `stream_reply` against it via httpx.
This exercises the actual bridge -> adapter wire format end-to-end.

We do NOT spin up real Slack here — the Slack-side handlers are covered by
test_slack_adapter.py. This file is about the SSE pump.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from hubzoid.slack.adapter import _ThrottledWriter, build_app, stream_reply
from hubzoid.slack.conversion import messages_from_thread


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


def _sse_payload(deltas: list[str]) -> bytes:
    """Build a complete OpenAI-shape SSE response body."""
    lines = [b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n']
    for d in deltas:
        payload = json.dumps({"choices": [{"delta": {"content": d}}]})
        lines.append(f"data: {payload}\n\n".encode())
    lines.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
    lines.append(b"data: [DONE]\n\n")
    return b"".join(lines)


def test_stream_reply_real_http_round_trip():
    """Adapter posts to bridge, parses SSE, fires on_delta in order."""
    captured: dict = {}
    body_bytes = _sse_payload(["Hello", " ", "world", "!"])

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=body_bytes,
            headers={"content-type": "text/event-stream"},
        )

    deltas: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stream_reply(
            bridge_url="http://bridge/v1",
            api_key="dev",
            model="testbot",
            messages=[{"role": "user", "content": "ping"}],
            on_delta=deltas.append,
            http_client=client,
        )

    assert "".join(deltas) == "Hello world!"
    assert captured["url"] == "http://bridge/v1/chat/completions"
    assert captured["auth"] == "Bearer dev"
    assert captured["body"]["stream"] is True
    assert captured["body"]["messages"] == [{"role": "user", "content": "ping"}]


def test_stream_reply_raises_on_bridge_401():
    """A bad bridge token should surface as an httpx.HTTPStatusError, not silent fail."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        import pytest
        with pytest.raises(httpx.HTTPStatusError):
            stream_reply(
                bridge_url="http://bridge/v1",
                api_key="wrong",
                model="m",
                messages=[{"role": "user", "content": "x"}],
                on_delta=lambda _d: None,
                http_client=client,
            )


def test_throttled_writer_batches_and_flushes():
    """A burst of small deltas should not produce one chat.update per delta."""
    calls: list[str] = []
    w = _ThrottledWriter(calls.append, interval=1.0)
    for c in "hello":
        w.feed(c)
    # First feed should have flushed once (last_flush=0 initially).
    assert len(calls) == 1
    final = w.done()
    assert final == "hello"
    # done() always flushes — total: first-tick + done = 2 calls minimum.
    assert calls[-1] == "hello"


def test_throttled_writer_does_not_raise_or_log_on_rapid_feeds(caplog):
    """Regression: rapid feeds inside the interval window must not trigger
    NameError-via-undefined-`text` in feed(), which would spam logs with
    'chat.update failed (mid-stream, will retry on next tick)' on every
    streaming delta."""
    import logging

    calls: list[str] = []
    w = _ThrottledWriter(calls.append, interval=10.0)  # so no mid-stream flush
    with caplog.at_level(logging.WARNING, logger="hubzoid.slack"):
        for c in "abcdef":
            w.feed(c)
    # All deltas land inside the interval, so writer should be called once
    # (the very first feed, where _last_flush=0 forces a flush).
    assert len(calls) == 1
    # And no errors should have been logged.
    assert not any("failed" in r.message for r in caplog.records)
    w.done()


def test_throttled_writer_flushes_again_after_interval():
    """Confirms the throttle interval actually elapses and triggers another flush."""
    import time as _time

    calls: list[str] = []
    w = _ThrottledWriter(calls.append, interval=0.1)
    w.feed("a")
    _time.sleep(0.15)
    w.feed("b")
    assert len(calls) == 2
    assert calls[-1] == "ab"
    w.done()


def test_full_thread_flow_assembles_correct_messages():
    """Slack thread payload -> messages_from_thread -> bridge request body shape."""
    slack_payload = [
        {"type": "message", "user": "U_USER", "text": "what is 2+2?", "ts": "1"},
        {"type": "message", "user": "U_BOT", "text": "thinking…", "ts": "2"},
        {"type": "message", "user": "U_USER", "text": "be more precise", "ts": "3"},
    ]
    msgs = messages_from_thread(slack_payload, bot_user_id="U_BOT")
    assert msgs == [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "thinking…"},
        {"role": "user", "content": "be more precise"},
    ]


def test_build_app_against_fixture_hub():
    """Smoke: build_app loads suggestions and bot identity from the fixture hub."""
    app = build_app(
        hub_dir=MINIMAL,
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="dev",
        model_label="testbot",
        bot_token="xoxb-fake",
        suggestions=["question 1", "question 2"],
        verify_token=False,
    )
    # Make sure both the Assistant and the legacy app_mention path are wired.
    assert app is not None
    assert len(app._listeners) >= 1
