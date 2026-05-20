"""Tests for the Slack adapter wiring (without spinning up real Slack).

We mock httpx so we can canned SSE bytes into stream_reply and assert the
adapter forwards each text delta. The adapter itself is built around two
testable seams: `stream_reply` (pure, takes a callback) and `build_app`
(returns a `slack_bolt.App` we can introspect).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from hubzoid.slack.adapter import build_app, stream_reply


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# ---------------------------------------------------------------------------
# stream_reply — the bridge -> Slack streaming pump
# ---------------------------------------------------------------------------
class _FakeSSEResponse:
    """Mimics enough of httpx.Response for stream_reply to consume."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self.status_code = 200

    def iter_lines(self):
        for line in self._lines:
            yield line.decode("utf-8") if isinstance(line, bytes) else line

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _sse_lines(*deltas: str) -> list[bytes]:
    out: list[bytes] = []
    for d in deltas:
        out.append(f'data: {{"choices":[{{"delta":{{"content":"{d}"}}}}]}}'.encode())
        out.append(b"")
    out.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    out.append(b"data: [DONE]")
    return out


def test_stream_reply_calls_on_delta_for_each_chunk():
    deltas: list[str] = []
    fake_client = MagicMock()
    fake_client.stream.return_value = _FakeSSEResponse(_sse_lines("hello", " ", "world"))

    stream_reply(
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="dev",
        model="testbot",
        messages=[{"role": "user", "content": "hi"}],
        on_delta=deltas.append,
        http_client=fake_client,
    )
    assert deltas == ["hello", " ", "world"]


def test_stream_reply_sends_bearer_auth_and_stream_true():
    fake_client = MagicMock()
    fake_client.stream.return_value = _FakeSSEResponse(_sse_lines("ok"))
    stream_reply(
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="my-key",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        on_delta=lambda _d: None,
        http_client=fake_client,
    )
    call = fake_client.stream.call_args
    # Positional: method, url
    assert call.args[0] == "POST"
    assert call.args[1] == "http://127.0.0.1:8000/v1/chat/completions"
    # Kwargs
    assert call.kwargs["headers"]["Authorization"] == "Bearer my-key"
    assert call.kwargs["json"]["stream"] is True
    assert call.kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert call.kwargs["json"]["model"] == "m"


def test_stream_reply_skips_role_only_and_done_chunks():
    """Ensures role-only and [DONE] chunks don't leak into on_delta."""
    deltas: list[str] = []
    fake_client = MagicMock()
    fake_client.stream.return_value = _FakeSSEResponse(
        [
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}',
            b"data: [DONE]",
        ]
    )
    stream_reply(
        bridge_url="http://x/v1",
        api_key="k",
        model="m",
        messages=[{"role": "user", "content": "ping"}],
        on_delta=deltas.append,
        http_client=fake_client,
    )
    assert deltas == ["hi"]


def test_stream_reply_raises_on_http_error():
    fake_client = MagicMock()
    err_resp = MagicMock()
    err_resp.status_code = 401
    err_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=err_resp
    )
    err_resp.iter_lines.return_value = iter([])
    err_resp.__enter__ = lambda self: self
    err_resp.__exit__ = lambda self, *a: False
    fake_client.stream.return_value = err_resp

    with pytest.raises(httpx.HTTPStatusError):
        stream_reply(
            bridge_url="http://x/v1",
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "ping"}],
            on_delta=lambda _d: None,
            http_client=fake_client,
        )


# ---------------------------------------------------------------------------
# build_app
# ---------------------------------------------------------------------------
def test_build_app_returns_slack_bolt_app():
    from slack_bolt import App

    app = build_app(
        hub_dir=MINIMAL,
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="dev",
        model_label="testbot",
        bot_token="xoxb-fake",
        suggestions=["ask one", "ask two"],
        verify_token=False,
    )
    assert isinstance(app, App)


def test_build_app_registers_app_mention_and_assistant():
    """Adapter must subscribe to app_mention and the Assistant lifecycle."""
    app = build_app(
        hub_dir=MINIMAL,
        bridge_url="http://127.0.0.1:8000/v1",
        api_key="dev",
        model_label="testbot",
        bot_token="xoxb-fake",
        suggestions=[],
        verify_token=False,
    )
    # slack-bolt stores matchers as opaque callables. We probe them by
    # synthesizing a fake event body of each type we care about and seeing
    # whether any registered listener matches.
    def _matches_any(body: dict) -> bool:
        for lst in app._listeners:
            for m in lst.matchers:
                try:
                    if m.func(body):
                        return True
                except Exception:
                    continue
        return False

    assert _matches_any(
        {"type": "event_callback", "event": {"type": "app_mention"}}
    ), "no app_mention listener"
    assert _matches_any(
        {"type": "event_callback", "event": {"type": "message", "channel_type": "im"}}
    ), "no DM (message.im) listener"
