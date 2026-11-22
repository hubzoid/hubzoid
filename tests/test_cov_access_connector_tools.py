"""Bounded MCP tool discovery, SSE parsing and session cleanup on failures."""
from __future__ import annotations

import contextlib
import time

import httpx
import pytest

from hubzoid.connectors import ConnectorError, server_tools as tools


@pytest.mark.parametrize("body,wanted", [(b'data: bad\n\ndata: {"id":2,"result":{}}\n\n', {"id": 2, "result": {}}),
    (b': heartbeat\n\ndata: {"id":1}\n\n', None), (b'data: []\n\n', None)])
def test_sse_selects_only_matching_json_rpc_id(body, wanted):
    assert tools._from_sse(body, 2) == wanted


@pytest.mark.parametrize("content,ctype,wanted", [(b'bad', "application/json", None),
    (b'[]', "application/json", None), (b'{"id":2}', "application/json", {"id": 2}),
    (b'data: {"id":2}\n\n', "text/event-stream", {"id": 2}),
    (b'data: bad\n\n', "text/event-stream", None)])
def test_message_parsing(content, ctype, wanted):
    r = httpx.Response(200, content=content, headers={"content-type": ctype}, request=httpx.Request("POST", "https://example.com"))
    assert tools._message(r, 2, time.monotonic() + 1) == wanted


def test_message_bounds_and_deadline(monkeypatch):
    r = httpx.Response(200, content=b"oversize", request=httpx.Request("POST", "https://example.com"))
    monkeypatch.setattr(tools, "MAX_BYTES", 2)
    assert tools._message(r, 2, time.monotonic() + 1) is None
    with pytest.raises(httpx.ReadTimeout, match="too long"):
        tools._message(r, 2, time.monotonic() - 1)


@pytest.mark.parametrize("failure,code", [("auth", "unauthorized"), ("status", "bad_response"),
    ("init", "bad_response"), ("list", "bad_response"), ("network", "unreachable"), (None, None)])
def test_discovery_failures_and_session_cleanup(monkeypatch, failure, code):
    calls = []
    class Client:
        @contextlib.contextmanager
        def stream(self, method, url, **kw):
            msg = kw.get("json", {})
            calls.append((method, msg, kw.get("headers")))
            if failure == "network":
                raise httpx.ConnectError("offline")
            if method == "DELETE":
                raise httpx.ReadError("cleanup unavailable")
            name = msg.get("method")
            payload, status, headers = {}, 200, {}
            if name == "initialize":
                status = 401 if failure == "auth" else 500 if failure == "status" else 200
                payload = {"id": 9 if failure == "init" else 1, "result": {}}
                headers = {"mcp-session-id": "session"}
            elif name == "tools/list":
                payload = {"id": msg["id"], "result": None if failure == "list" else {
                    "tools": [{"name": "search", "description": "  Find\n records  "}, {"name": "other", "description": 3}, "bad"],
                    "nextCursor": "next" if msg["id"] == 2 else None}}
            yield httpx.Response(status, json=payload, headers=headers, request=httpx.Request(method, url))
    @contextlib.contextmanager
    def client(url):
        yield Client()
    monkeypatch.setattr(tools.net, "client", client)
    if code:
        with pytest.raises(ConnectorError) as exc:
            tools.list_tools("https://example.com/mcp")
        assert exc.value.code == code
    else:
        assert tools.list_tools("https://example.com/mcp") == [
            {"name": "search", "description": "Find records"}, {"name": "other", "description": ""}] * 2
        assert calls[3][1]["params"] == {"cursor": "next"}
    if failure in (None, "list"):
        assert calls[-1][0] == "DELETE" and calls[-1][2]["mcp-session-id"] == "session"
