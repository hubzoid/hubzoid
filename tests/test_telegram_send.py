"""Telegram outbound via sendMessage, HTML parse mode."""
import json

import httpx

from hubzoid.telegram.send import send_message


def test_send_message_hits_bot_endpoint_with_html():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    send_message(token="BOTTOKEN", chat_id="42", text="<b>hi</b>", http_client=client)
    assert "/botBOTTOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "42"
    assert captured["body"]["text"] == "<b>hi</b>"
    assert captured["body"]["parse_mode"] == "HTML"
