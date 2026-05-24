"""Sidecar-metadata tests for upload ingestion.

When the bridge writes an upload (data-URL or POST /uploads), it should
also drop a `{filename}.hubzoid.json` sidecar holding {mime, size, kind}.
Downstream tools read this instead of re-sniffing every time, and
listings hide it from the agent.
"""
from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture(autouse=True)
def _cleanup_chats():
    yield
    shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    yield


@pytest.fixture
def client(env):
    from hubzoid.server import build_app
    return TestClient(build_app())


def _read_sidecar(uploads: Path, filename: str) -> dict:
    return json.loads((uploads / f"{filename}.hubzoid.json").read_text())


# ---------------------------------------------------------------------------
# classify(): mime + payload -> kind
# ---------------------------------------------------------------------------
class TestClassify:
    def test_image_mime(self):
        from hubzoid.uploads import classify
        assert classify("image/png", b"\x89PNG\r\n\x1a\n") == "image"
        assert classify("image/jpeg", b"\xff\xd8\xff\xe0") == "image"

    def test_pdf_mime(self):
        from hubzoid.uploads import classify
        assert classify("application/pdf", b"%PDF-1.7\n") == "pdf"

    def test_json_mime(self):
        from hubzoid.uploads import classify
        assert classify("application/json", b'{"a":1}') == "json"

    def test_csv_mime(self):
        from hubzoid.uploads import classify
        assert classify("text/csv", b"a,b\n1,2\n") == "csv"

    def test_plain_text_mime(self):
        from hubzoid.uploads import classify
        assert classify("text/plain", b"hello world\n") == "text"

    def test_octet_stream_with_text_payload_classifies_as_text(self):
        """Generic mime + utf-8 decodable -> text."""
        from hubzoid.uploads import classify
        assert classify("application/octet-stream", b"hello world\n") == "text"

    def test_octet_stream_with_binary_payload_classifies_as_binary(self):
        from hubzoid.uploads import classify
        assert classify("application/octet-stream", b"\x00\x01\x02\xff\xfe") == "binary"


# ---------------------------------------------------------------------------
# Sidecar written via data-URL ingest path
# ---------------------------------------------------------------------------
def test_data_url_attachment_writes_sidecar(client):
    async def capture(self, prompt):
        return "ok"

    payload = b'{"hello": "world"}'
    b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:application/json;name=blob.json;base64,{b64}"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "chat_id": "sidecar-c",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "here"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
        )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/sidecar-c/uploads"
    # Find the non-sidecar file.
    actuals = [p for p in uploads.iterdir() if not p.name.endswith(".hubzoid.json")]
    assert len(actuals) == 1
    fname = actuals[0].name
    meta = _read_sidecar(uploads, fname)
    assert meta["mime"] == "application/json"
    assert meta["size"] == len(payload)
    assert meta["kind"] == "json"


# ---------------------------------------------------------------------------
# Sidecar written via POST /uploads
# ---------------------------------------------------------------------------
def test_post_upload_writes_sidecar_with_kind_text(client):
    r = client.post(
        "/uploads/text-chat/notes.txt",
        headers={"Authorization": "Bearer dev", "Content-Type": "text/plain"},
        content=b"hello there\nline two\n",
    )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/text-chat/uploads"
    meta = _read_sidecar(uploads, "notes.txt")
    assert meta["kind"] == "text"
    assert meta["mime"] == "text/plain"


def test_post_upload_sidecar_falls_back_to_content_type_header(client):
    """If no header provided, mime is sniffed from the filename extension."""
    r = client.post(
        "/uploads/sniff-chat/data.csv",
        headers={"Authorization": "Bearer dev"},
        content=b"a,b,c\n1,2,3\n",
    )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/sniff-chat/uploads"
    meta = _read_sidecar(uploads, "data.csv")
    assert meta["mime"] == "text/csv"
    assert meta["kind"] == "csv"


def test_post_upload_binary_classified_as_binary(client):
    r = client.post(
        "/uploads/bin-chat/blob.bin",
        headers={"Authorization": "Bearer dev", "Content-Type": "application/octet-stream"},
        content=b"\x00\x01\x02\xff\xfe",
    )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/bin-chat/uploads"
    meta = _read_sidecar(uploads, "blob.bin")
    assert meta["kind"] == "binary"


def test_post_upload_pdf_classified_as_pdf(client):
    r = client.post(
        "/uploads/pdf-chat/doc.pdf",
        headers={"Authorization": "Bearer dev", "Content-Type": "application/pdf"},
        content=b"%PDF-1.7\n%minimal\n",
    )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/pdf-chat/uploads"
    meta = _read_sidecar(uploads, "doc.pdf")
    assert meta["kind"] == "pdf"
