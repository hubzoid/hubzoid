"""Size-cap tests for chat-completion data-URL attachments and POST /uploads.

The cap is configured via HUBZOID_MAX_UPLOAD_BYTES (settings.max_upload_bytes,
default 25 MiB). Bytes-over-cap from either ingress path must be rejected
with 413 — silently truncating would let the agent reason over a half file.
"""
from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture(autouse=True)
def _cleanup_chats():
    yield
    shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


@pytest.fixture
def small_cap_env(monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    # 1 KiB cap — easy to exceed with a tiny payload.
    monkeypatch.setenv("HUBZOID_MAX_UPLOAD_BYTES", "1024")
    yield


@pytest.fixture
def client(small_cap_env):
    from hubzoid.server import build_app
    return TestClient(build_app())


def test_settings_default_max_upload_bytes_is_25_mib(monkeypatch):
    """Default cap is 25 MiB when env var is unset."""
    monkeypatch.delenv("HUBZOID_MAX_UPLOAD_BYTES", raising=False)
    from hubzoid import settings as settingslib
    s = settingslib.load(MINIMAL)
    assert s.max_upload_bytes == 25 * 1024 * 1024


def test_settings_honors_max_upload_bytes_env(monkeypatch):
    monkeypatch.setenv("HUBZOID_MAX_UPLOAD_BYTES", "4096")
    from hubzoid import settings as settingslib
    s = settingslib.load(MINIMAL)
    assert s.max_upload_bytes == 4096


def test_post_upload_rejects_oversize_body(client):
    """POST /uploads with a body bigger than the cap returns 413."""
    payload = b"x" * 2048  # cap is 1024
    r = client.post(
        "/uploads/oversize-chat/big.bin",
        headers={"Authorization": "Bearer dev"},
        content=payload,
    )
    assert r.status_code == 413, r.text
    # Cap is communicated in the error so the caller can adjust.
    assert "1024" in r.text


def test_post_upload_accepts_under_cap(client):
    payload = b"x" * 512
    r = client.post(
        "/uploads/ok-chat/small.bin",
        headers={"Authorization": "Bearer dev"},
        content=payload,
    )
    assert r.status_code == 200
    assert (MINIMAL / ".hubzoid/chats/ok-chat/uploads/small.bin").read_bytes() == payload


def test_chat_completion_rejects_oversize_data_url_attachment(client):
    """A data: url decoding to > cap bytes must 413 the whole request."""
    from unittest.mock import patch

    payload = b"x" * 2048  # cap is 1024
    b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:application/octet-stream;base64,{b64}"

    async def capture(self, prompt):
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "chat_id": "over-cap",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
        )
    assert r.status_code == 413, r.text
    # Nothing should have been written for the rejected chat.
    assert not (MINIMAL / ".hubzoid/chats/over-cap/uploads").exists()


def test_chat_completion_allows_attachment_under_cap(client):
    """Under-cap data URL still flows through and lands in uploads/."""
    from unittest.mock import patch

    payload = b"y" * 512
    b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:application/octet-stream;base64,{b64}"

    async def capture(self, prompt):
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "chat_id": "under-cap",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            },
        )
    assert r.status_code == 200
    uploads = MINIMAL / ".hubzoid/chats/under-cap/uploads"
    files = [p for p in uploads.iterdir() if not p.name.endswith(".hubzoid.json")]
    assert len(files) == 1
    assert files[0].read_bytes() == payload
