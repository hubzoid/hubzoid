"""The dispatch pump: POST to the bridge's /v1/chat/completions, forward the
resolved identity as headers (the same contract Slack uses), and collect the
streamed reply into one string (webhook surfaces can't stream-edit)."""
import json

import httpx

from hubzoid.inbound.dispatch import dispatch, parse_sse_delta


def test_parse_sse_delta_extracts_content():
    assert parse_sse_delta('data: {"choices":[{"delta":{"content":"hi"}}]}') == "hi"


def test_parse_sse_delta_ignores_done_and_role_only():
    assert parse_sse_delta("data: [DONE]") is None
    assert parse_sse_delta('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
    assert parse_sse_delta("") is None


def test_dispatch_collects_reply_and_forwards_identity_headers():
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        sse = (
            'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"Ravi"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = dispatch(
        bridge_url="http://x/v1", api_key="k", model="m",
        messages=[{"role": "user", "content": "hi"}], surface="whatsapp",
        user_email="ravi@isha.org", groups=["coordinator"], chat_id="wa-42",
        http_client=client,
    )
    assert out == "Hello Ravi"
    assert captured["headers"]["x-openwebui-user-email"] == "ravi@isha.org"
    assert captured["headers"]["x-hubzoid-surface"] == "whatsapp"
    assert captured["headers"]["x-hubzoid-groups"] == "coordinator"
    assert captured["headers"]["authorization"] == "Bearer k"
    assert captured["body"]["model"] == "m"
    assert captured["body"]["chat_id"] == "wa-42"
    assert captured["body"]["stream"] is True


def test_dispatch_omits_identity_headers_when_absent():
    def handler(request):
        assert "x-openwebui-user-email" not in request.headers
        assert "x-hubzoid-groups" not in request.headers
        return httpx.Response(200, content="data: [DONE]\n\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert dispatch(bridge_url="http://x/v1", api_key="k", model="m",
                    messages=[], surface="telegram", http_client=client) == ""
