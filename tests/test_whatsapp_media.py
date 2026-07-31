"""WhatsApp inbound media: the parser surfaces attachments as MediaRefs, and
`fetch` runs Meta's two-step download (media id -> signed URL -> bytes)."""
import httpx

from hubzoid.whatsapp.media import fetch
from hubzoid.whatsapp.parse import parse_messages
from hubzoid.inbound.message import MediaRef


def _wrap(msg):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "919", "profile": {"name": "Ravi"}}],
        "messages": [msg],
    }}]}]}


def test_image_message_becomes_message_with_media_and_caption():
    payload = _wrap({
        "id": "wamid.1", "from": "919", "type": "image",
        "image": {"id": "MID7", "mime_type": "image/jpeg", "caption": "look at this"},
    })
    [m] = parse_messages(payload)
    assert m.text == "look at this"
    assert len(m.media) == 1
    ref = m.media[0]
    assert ref.key == "MID7"
    assert ref.mime == "image/jpeg"
    assert ref.name.endswith(".jpg")


def test_image_without_caption_still_answerable():
    payload = _wrap({
        "id": "wamid.2", "from": "919", "type": "image",
        "image": {"id": "MID8", "mime_type": "image/png"},
    })
    [m] = parse_messages(payload)
    assert m.text == ""            # no caption, but not dropped
    assert m.media[0].key == "MID8"


def test_document_uses_provided_filename():
    payload = _wrap({
        "id": "wamid.3", "from": "919", "type": "document",
        "document": {"id": "DOC1", "mime_type": "application/pdf", "filename": "invoice.pdf"},
    })
    [m] = parse_messages(payload)
    assert m.media[0].name == "invoice.pdf"


def test_plain_text_message_has_no_media():
    payload = _wrap({"id": "wamid.4", "from": "919", "type": "text",
                     "text": {"body": "hello"}})
    [m] = parse_messages(payload)
    assert m.text == "hello"
    assert m.media == ()


def test_fetch_two_step_download():
    def handler(request):
        url = str(request.url)
        assert request.headers["authorization"] == "Bearer TKN"
        if "graph.facebook.com" in url and url.endswith("/MID7"):
            return httpx.Response(200, json={
                "url": "https://lookaside.fbsbx.com/media/blob123",
                "mime_type": "image/jpeg",
            })
        if "lookaside.fbsbx.com/media/blob123" in url:
            return httpx.Response(200, content=b"\xff\xd8realjpeg")
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    ref = MediaRef(key="MID7", name="image-MID7.jpg", mime="image/jpeg")
    assert fetch(ref, token="TKN", http=http) == b"\xff\xd8realjpeg"


def test_fetch_returns_none_on_missing_url():
    def handler(request):
        return httpx.Response(200, json={"mime_type": "image/jpeg"})  # no url
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch(MediaRef(key="X", name="x.jpg"), token="T", http=http) is None


def test_fetch_returns_none_on_error():
    def handler(request):
        return httpx.Response(500)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch(MediaRef(key="X", name="x.jpg"), token="T", http=http) is None
