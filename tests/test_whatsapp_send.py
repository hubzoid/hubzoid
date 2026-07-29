"""WhatsApp outbound via the Meta Cloud API: text (session) and template
(proactive). Bearer token, JSON body shaped as Meta expects."""
import json

import httpx

from hubzoid.whatsapp.send import send_template, send_text


def test_send_text_posts_to_graph_with_bearer_and_body():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    send_text(phone_number_id="PNID", token="TKN", to="919800000001",
              text="hi", http_client=client)
    assert "PNID/messages" in captured["url"]
    assert captured["auth"] == "Bearer TKN"
    assert captured["body"]["to"] == "919800000001"
    assert captured["body"]["type"] == "text"
    assert captured["body"]["text"]["body"] == "hi"


def test_send_template_shapes_template_payload():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    send_template(phone_number_id="PNID", token="TKN", to="919800000001",
                  template="daily_digest", language="en", http_client=client)
    assert captured["body"]["type"] == "template"
    assert captured["body"]["template"]["name"] == "daily_digest"
    assert captured["body"]["template"]["language"]["code"] == "en"
