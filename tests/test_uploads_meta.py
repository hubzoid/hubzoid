"""Sidecar-metadata tests for upload ingestion.

When a stored file is attached to a chat (`hubzoid.attachments.attach`), the
chat's folder also gets a `{filename}.hubzoid.json` sidecar holding
{mime, size, kind}.
Downstream tools read this instead of re-sniffing every time, and
listings hide it from the agent.
"""
from __future__ import annotations

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


from tests.access_helpers import caller  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
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
# Sidecar written when a stored file is attached
# ---------------------------------------------------------------------------
def _attach(client, name: str, payload: bytes, mime: str, chat="meta-chat") -> Path:
    up = client.post("/api/chat/files", headers={"Authorization": "Bearer dev"},
                     files={"file": (name, payload, mime)})
    assert up.status_code == 201, up.text

    async def fake_run(self, prompt):
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=fake_run):
        r = client.post("/v1/chat/completions", headers=caller(MINIMAL), json={
            "model": "testbot-label", "chat_id": chat, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "see"}, {"type": "file", "file": {"file_id": up.json()["file_id"]}}]}]})
    assert r.status_code == 200, r.text
    return MINIMAL / ".hubzoid" / "chats" / chat / "uploads"


def test_attach_writes_sidecar_with_kind_text(client):
    uploads = _attach(client, "notes.txt", b"hello world\n", "text/plain")
    assert _read_sidecar(uploads, "notes.txt") == {"mime": "text/plain", "size": 12, "kind": "text"}


def test_sidecar_type_falls_back_to_the_name(client):
    uploads = _attach(client, "readme.md", b"# hi\n", "application/octet-stream")
    assert _read_sidecar(uploads, "readme.md")["mime"] == "text/markdown"


def test_binary_classified_as_binary(client):
    uploads = _attach(client, "blob.bin", b"\x00\x01\x02\xff\xfe", "application/octet-stream")
    assert _read_sidecar(uploads, "blob.bin")["kind"] == "binary"


def test_pdf_classified_as_pdf(client):
    uploads = _attach(client, "spec.pdf", b"%PDF-1.7\n%fake\n", "application/pdf")
    assert _read_sidecar(uploads, "spec.pdf")["kind"] == "pdf"
