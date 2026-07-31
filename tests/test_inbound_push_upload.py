"""The generic attachment layer: push bytes to the bridge's per-chat uploads
route and return the text marker to stitch into the user turn. Shared by every
inbound surface (WhatsApp, Telegram) — the same store+marker+vision pipeline the
web and Slack already use."""
import httpx

from hubzoid.inbound.uploads import push_upload


def _client(captured, status=200):
    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["ctype"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(status, json={"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_image_returns_image_marker_and_posts_to_uploads():
    cap = {}
    marker = push_upload(
        http=_client(cap), bridge_url="http://127.0.0.1:8000/v1", api_key="KEY",
        chat_id="whatsapp-919", name="photo-7.jpg", mime="image/jpeg",
        content=b"\xff\xd8jpegbytes", max_upload_bytes=1_000_000,
    )
    # Canonical [Image: name] reference -> vision_inject expands it to a real
    # image block at model-call time.
    assert marker.startswith("[Image: photo-7.jpg]")
    assert cap["url"] == "http://127.0.0.1:8000/uploads/whatsapp-919/photo-7.jpg"
    assert cap["auth"] == "Bearer KEY"
    assert cap["ctype"] == "image/jpeg"
    assert cap["body"] == b"\xff\xd8jpegbytes"


def test_non_image_returns_read_upload_note():
    cap = {}
    marker = push_upload(
        http=_client(cap), bridge_url="http://127.0.0.1:8000/v1", api_key="KEY",
        chat_id="telegram-42", name="report.pdf", mime="application/pdf",
        content=b"%PDF-1.4", max_upload_bytes=1_000_000,
    )
    assert "report.pdf" in marker
    assert "read_upload('report.pdf')" in marker
    assert "[Image:" not in marker


def test_oversize_is_skipped_without_posting():
    cap = {}
    marker = push_upload(
        http=_client(cap), bridge_url="http://127.0.0.1:8000/v1", api_key="KEY",
        chat_id="c", name="big.bin", mime="application/octet-stream",
        content=b"x" * 100, max_upload_bytes=10,
    )
    assert marker is None
    assert cap == {}  # never hit the bridge


def test_bridge_error_returns_none():
    cap = {}
    marker = push_upload(
        http=_client(cap, status=500), bridge_url="http://127.0.0.1:8000/v1",
        api_key="KEY", chat_id="c", name="a.png", mime="image/png",
        content=b"\x89PNG", max_upload_bytes=1_000_000,
    )
    assert marker is None


def test_bridge_url_without_v1_suffix_is_handled():
    cap = {}
    push_upload(
        http=_client(cap), bridge_url="http://127.0.0.1:8000", api_key="KEY",
        chat_id="c", name="a.png", mime="image/png", content=b"\x89PNG",
        max_upload_bytes=1_000_000,
    )
    assert cap["url"] == "http://127.0.0.1:8000/uploads/c/a.png"


def test_port_ending_in_one_is_not_mangled():
    # A naive rstrip('/v1') would eat the trailing '1' of the port. Guard it.
    cap = {}
    push_upload(
        http=_client(cap), bridge_url="http://127.0.0.1:8001/v1", api_key="KEY",
        chat_id="c", name="a.png", mime="image/png", content=b"\x89PNG",
        max_upload_bytes=1_000_000,
    )
    assert cap["url"] == "http://127.0.0.1:8001/uploads/c/a.png"
