"""Size-cap tests for the one upload route, POST /api/chat/files.

The cap is configured via HUBZOID_MAX_UPLOAD_BYTES (settings.max_upload_bytes,
default 25 MiB). Bytes over the cap are rejected with 413 — silently
truncating would let the agent reason over a half file.
"""
from __future__ import annotations

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
def small_cap_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    # 1 KiB cap — easy to exceed with a tiny payload.
    monkeypatch.setenv("HUBZOID_MAX_UPLOAD_BYTES", "1024")
    yield


from tests.access_helpers import caller  # noqa: E402


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


KEY = {"Authorization": "Bearer dev"}


def test_upload_rejects_oversize_body(client):
    r = client.post("/api/chat/files", headers=KEY, files={"file": ("big.txt", b"x" * 2048, "text/plain")})
    assert r.status_code == 413, r.text
    assert not (MINIMAL / ".hubzoid" / "chats").exists()


def test_upload_accepts_under_cap_and_a_file_part_attaches_it(client):
    r = client.post("/api/chat/files", headers=KEY, files={"file": ("ok.txt", b"x" * 512, "text/plain")})
    assert r.status_code == 201, r.text
    from unittest.mock import patch

    async def fake_run(self, prompt):
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=fake_run):
        done = client.post("/v1/chat/completions", headers=caller(MINIMAL), json={
            "model": "testbot-label", "chat_id": "under-cap", "messages": [{"role": "user", "content": [
                {"type": "text", "text": "read"}, {"type": "file", "file": {"file_id": r.json()["file_id"]}}]}]})
    assert done.status_code == 200, done.text
    assert (MINIMAL / ".hubzoid/chats/under-cap/uploads/ok.txt").read_bytes() == b"x" * 512
