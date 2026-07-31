"""Telegram inbound media: the parser surfaces photos/documents/voice as
MediaRefs (with any caption as the text), and `fetch` runs the getFile ->
download two-step."""
import httpx

from hubzoid.telegram.media import fetch
from hubzoid.telegram.parse import parse_update
from hubzoid.inbound.message import MediaRef


def _update(message):
    return {"update_id": 5, "message": {"from": {"id": 42, "first_name": "Ravi"}, **message}}


def test_photo_becomes_text_kind_with_media_and_caption():
    # Telegram sends an array of sizes; we take the largest (last).
    p = parse_update(_update({
        "caption": "my receipt",
        "photo": [
            {"file_id": "small", "file_unique_id": "us", "width": 90},
            {"file_id": "big", "file_unique_id": "ub", "width": 1280},
        ],
    }))
    assert p.kind == "text"
    assert p.text == "my receipt"
    assert len(p.media) == 1
    assert p.media[0].key == "big"
    assert p.media[0].mime == "image/jpeg"
    assert p.media[0].name.endswith(".jpg")


def test_photo_without_caption_still_answerable():
    p = parse_update(_update({"photo": [{"file_id": "big", "file_unique_id": "ub"}]}))
    assert p.kind == "text"
    assert p.text == ""
    assert p.media[0].key == "big"


def test_document_uses_filename_and_mime():
    p = parse_update(_update({
        "document": {"file_id": "D1", "file_name": "report.pdf", "mime_type": "application/pdf"},
    }))
    assert p.kind == "text"
    assert p.media[0].name == "report.pdf"
    assert p.media[0].mime == "application/pdf"


def test_voice_note_is_media():
    p = parse_update(_update({"voice": {"file_id": "V1", "mime_type": "audio/ogg"}}))
    assert p.kind == "text"
    assert p.media[0].key == "V1"
    assert p.media[0].name.endswith(".ogg")


def test_plain_text_has_no_media():
    p = parse_update(_update({"text": "hello"}))
    assert p.kind == "text"
    assert p.media == ()


def test_fetch_two_step_download():
    def handler(request):
        url = str(request.url)
        if "/getFile" in url:
            assert "file_id=D1" in url
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/f.pdf"}})
        if url.endswith("/file/botTKN/docs/f.pdf"):
            return httpx.Response(200, content=b"%PDF-real")
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    ref = MediaRef(key="D1", name="report.pdf", mime="application/pdf")
    assert fetch(ref, token="TKN", http=http) == b"%PDF-real"


def test_fetch_returns_none_when_getfile_fails():
    def handler(request):
        return httpx.Response(200, json={"ok": False})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch(MediaRef(key="D1", name="x"), token="T", http=http) is None
