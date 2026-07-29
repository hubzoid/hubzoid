"""WhatsApp read receipts + typing indicator. Marking an incoming message read
gives blue ticks; adding typing_indicator also shows 'typing…' (~25s or until we
send the reply). One call to /{phone_number_id}/messages does both."""
import json

import httpx

from hubzoid.whatsapp.send import mark_read


def _client(captured):
    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_mark_read_with_typing_posts_status_and_indicator():
    cap = {}
    mark_read(phone_number_id="PNID", token="TKN", message_id="wamid.X",
              typing=True, http_client=_client(cap))
    assert "PNID/messages" in cap["url"]
    assert cap["auth"] == "Bearer TKN"
    assert cap["body"]["status"] == "read"
    assert cap["body"]["message_id"] == "wamid.X"
    assert cap["body"]["typing_indicator"] == {"type": "text"}


def test_mark_read_without_typing_omits_indicator():
    cap = {}
    mark_read(phone_number_id="PNID", token="TKN", message_id="wamid.X",
              typing=False, http_client=_client(cap))
    assert cap["body"]["status"] == "read"
    assert "typing_indicator" not in cap["body"]
