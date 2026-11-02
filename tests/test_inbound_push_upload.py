"""The one way a channel hands over a file: upload the bytes to the bridge's
internal POST /v1/files with the bridge key and get the stored file id, which the
message then names as a file part. Shared by Slack, WhatsApp and Telegram."""
import httpx

from hubzoid.inbound.uploads import file_part, push_upload, upload_url, with_files


def _client(captured, status=201, body=None):
    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["ctype"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(status, json=body if body is not None else {"file_id": "f-123"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_upload_posts_one_multipart_file_and_returns_the_file_id():
    cap = {}
    got = push_upload(http=_client(cap), bridge_url="http://127.0.0.1:8000/v1", api_key="KEY",
                      name="photo-7.jpg", mime="image/jpeg", content=b"\xff\xd8jpegbytes",
                      max_upload_bytes=1_000_000)
    assert got == "f-123"
    assert cap["url"] == "http://127.0.0.1:8000/v1/files"
    assert cap["auth"] == "Bearer KEY"
    assert cap["ctype"].startswith("multipart/form-data")
    assert b'filename="photo-7.jpg"' in cap["body"] and b"\xff\xd8jpegbytes" in cap["body"]
    assert b"Content-Type: image/jpeg" in cap["body"]


def test_oversize_is_skipped_without_posting():
    cap = {}
    assert push_upload(http=_client(cap), bridge_url="http://b/v1", api_key="K", name="big.bin",
                       mime="application/octet-stream", content=b"x" * 11, max_upload_bytes=10) is None
    assert "url" not in cap


def test_bridge_error_or_no_id_returns_none():
    for status, body in ((413, {"detail": "too big"}), (201, {"nope": 1})):
        assert push_upload(http=_client({}, status, body), bridge_url="http://b/v1", api_key="K", name="a.txt",
                           mime="text/plain", content=b"a", max_upload_bytes=10) is None


def test_upload_url_strips_only_the_v1_suffix():
    assert upload_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1/files"
    assert upload_url("http://127.0.0.1:8001/v1") == "http://127.0.0.1:8001/v1/files"   # port ends in 1
    assert upload_url("http://127.0.0.1:8001/") == "http://127.0.0.1:8001/v1/files"


def test_messages_name_files_as_parts():
    assert with_files("hi", []) == "hi"
    assert with_files("see", ["f1", "f2"]) == [{"type": "text", "text": "see"}, file_part("f1"), file_part("f2")]
    assert with_files("", ["f1"]) == [{"type": "file", "file": {"file_id": "f1"}}]
