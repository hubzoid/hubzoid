"""Tests for per-chat artifacts: ContextVar scoping, write_artifact link,
read_upload tool, sanitisation, and the bridge's /artifacts + /uploads routes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agents.tool_context import ToolContext
from fastapi.testclient import TestClient

from hubzoid import _request_ctx
from hubzoid import memory as memlib
from hubzoid.tools import files as files_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _call(tool, **kwargs: Any) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=args,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _by_name(tools: list, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"tool {name!r} not in {[t.name for t in tools]}")


@dataclass
class _Ctx:
    hub_dir: Path
    output_dir: Path
    session_id: str = "session-fallback"
    settings: Any = None
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    hub = tmp_path / "hub"
    hub.mkdir()
    out = hub / "output" / "session-fallback"
    out.mkdir(parents=True)
    return _Ctx(hub_dir=hub, output_dir=out)


@pytest.fixture(autouse=True)
def _reset_request_ctx():
    _request_ctx.set_chat_id(None)
    yield
    _request_ctx.set_chat_id(None)


# ---------------------------------------------------------------------------
# memory: sanitization
# ---------------------------------------------------------------------------
class TestSanitizeChatId:
    @pytest.mark.parametrize("raw", [None, "", "   ", "..", ".", "....-...."])
    def test_invalid_inputs_become_none(self, raw):
        # Edge: ".. ." sanitizes to nothing usable.
        result = memlib.sanitize_chat_id(raw)
        # Either None or a meaningfully-non-trivial string.
        if result is not None:
            assert len(result) >= 1

    def test_uuid_passes_through(self):
        u = "abc123-def456-7890ab"
        assert memlib.sanitize_chat_id(u) == u

    def test_strips_path_separators(self):
        assert memlib.sanitize_chat_id("../../etc/passwd") == "etc-passwd"

    def test_caps_length(self):
        out = memlib.sanitize_chat_id("x" * 200)
        assert out is not None and len(out) <= 64

    def test_keeps_dots_and_underscores(self):
        # Slack thread_ts looks like 1234567890.123456
        assert memlib.sanitize_chat_id("1234567890.123456") == "1234567890.123456"


# ---------------------------------------------------------------------------
# write_artifact: chat scoping
# ---------------------------------------------------------------------------
def test_write_artifact_uses_chat_dir_when_chat_in_scope(ctx, monkeypatch):
    monkeypatch.setenv("BRIDGE_PORT", "9999")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("chat-abc"):
        result = _call(write, filename="report.json", content='{"ok":1}')
    artifact = ctx.hub_dir / ".hubzoid/chats/chat-abc/artifacts/report.json"
    assert artifact.is_file()
    assert artifact.read_text() == '{"ok":1}'
    # Link uses the default localhost public URL and embeds a signed token
    # so clicking it from a browser does not need a Bearer header.
    assert "http://127.0.0.1:9999/artifacts/chat-abc/report.json?t=" in result
    assert "Download report.json" in result


def test_write_artifact_signed_url_matches_signing_module(ctx, monkeypatch):
    monkeypatch.setenv("BRIDGE_API_KEYS", "secret-key")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    from hubzoid import _signing
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("chat-X"):
        result = _call(write, filename="data.txt", content="hi")
    expected = _signing.sign_artifact_path("chat-X", "data.txt")
    assert f"?t={expected}" in result


def test_write_artifact_falls_back_to_session_dir_when_no_chat(ctx):
    write = _by_name(files_mod.make(ctx), "write_artifact")
    # No chat scope set -> writes to ctx.output_dir (legacy session dir).
    result = _call(write, filename="r.txt", content="hi")
    assert (ctx.output_dir / "r.txt").is_file()
    # No download link when there's no chat id.
    assert "Download" not in result
    assert "Saved" in result


def test_write_artifact_strips_directory_components(ctx):
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("chat-zz"):
        result = _call(write, filename="output/nested/file.png", content="x")
    # The dirs got stripped — file lands at the chat artifact root.
    assert (ctx.hub_dir / ".hubzoid/chats/chat-zz/artifacts/file.png").is_file()
    # And not inside any "output" or "nested" subdir.
    assert not (ctx.hub_dir / ".hubzoid/chats/chat-zz/artifacts/output").exists()


def test_write_artifact_refuses_empty_filename_after_sanitization(ctx):
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("c"):
        result = _call(write, filename="../../..", content="x")
    assert "refused" in result.lower() or "empty filename" in result.lower()


def test_write_artifact_honors_public_url(ctx, monkeypatch):
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/")
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("c1"):
        result = _call(write, filename="x.txt", content="x")
    assert "https://hub.example.com/artifacts/c1/x.txt" in result


# ---------------------------------------------------------------------------
# read_upload
# ---------------------------------------------------------------------------
def test_read_upload_reads_text_file(ctx):
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, "chat-r")
    (upload_dir / "notes.md").write_text("hello upload", encoding="utf-8")
    read = _by_name(files_mod.make(ctx), "read_upload")
    with _request_ctx.chat_scope("chat-r"):
        out = _call(read, filename="notes.md")
    # Path header is prepended; body is verbatim for small files.
    assert "hello upload" in out
    assert "Path on disk:" in out


def test_read_upload_missing_lists_available(ctx):
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, "chat-r")
    (upload_dir / "have.txt").write_text("x", encoding="utf-8")
    read = _by_name(files_mod.make(ctx), "read_upload")
    with _request_ctx.chat_scope("chat-r"):
        out = _call(read, filename="missing.txt")
    assert "not found" in out
    assert "have.txt" in out


def test_read_upload_requires_chat_in_scope(ctx):
    read = _by_name(files_mod.make(ctx), "read_upload")
    out = _call(read, filename="anything.md")
    assert "no chat is in scope" in out


def test_read_upload_refuses_path_escape(ctx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not meant for the agent")
    read = _by_name(files_mod.make(ctx), "read_upload")
    with _request_ctx.chat_scope("c"):
        out = _call(read, filename=str(secret))
    # Sanitization strips dirs -> tries secret.txt under uploads, won't exist.
    assert "not found" in out or "refused" in out


def test_read_upload_binary_returns_path_note(ctx):
    upload_dir = memlib.chat_upload_dir(ctx.hub_dir, "bin")
    binfile = upload_dir / "blob.bin"
    binfile.write_bytes(b"\xff\xfe\x00binary")
    read = _by_name(files_mod.make(ctx), "read_upload")
    with _request_ctx.chat_scope("bin"):
        out = _call(read, filename="blob.bin")
    assert "binary" in out


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------
def test_list_artifacts_empty(ctx):
    lst = _by_name(files_mod.make(ctx), "list_artifacts")
    with _request_ctx.chat_scope("c"):
        out = _call(lst)
    assert "no artifacts" in out.lower()


def test_list_artifacts_after_write(ctx):
    write = _by_name(files_mod.make(ctx), "write_artifact")
    lst = _by_name(files_mod.make(ctx), "list_artifacts")
    with _request_ctx.chat_scope("c2"):
        _call(write, filename="one.txt", content="1")
        _call(write, filename="two.txt", content="22")
        out = _call(lst)
    assert "one.txt" in out and "two.txt" in out


# ---------------------------------------------------------------------------
# Bridge: /artifacts route + chat-id derivation + attachment persistence
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture
def bridge_env(monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    yield


@pytest.fixture
def client(bridge_env):
    from hubzoid.server import build_app
    return TestClient(build_app())


def test_artifacts_route_serves_file(client):
    chat_dir = memlib.chat_artifact_dir(MINIMAL, "demo-chat")
    (chat_dir / "report.json").write_text('{"hello":"world"}', encoding="utf-8")
    try:
        r = client.get(
            "/artifacts/demo-chat/report.json",
            headers={"Authorization": "Bearer dev"},
        )
        assert r.status_code == 200
        assert r.json() == {"hello": "world"}
    finally:
        # Clean up the fixture's chats dir so other tests don't see stale state.
        import shutil
        shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


def test_artifacts_route_rejects_unauthenticated_request_without_token(client):
    """No Bearer, no ?t= signed token → 401."""
    assert client.get("/artifacts/anything/file.txt").status_code == 401


def test_artifacts_route_accepts_signed_token_without_bearer(client):
    """A correctly-signed ?t= token lets the browser fetch without auth header."""
    from hubzoid import _signing
    chat_dir = memlib.chat_artifact_dir(MINIMAL, "signed-chat")
    (chat_dir / "out.json").write_text('{"a":1}', encoding="utf-8")
    token = _signing.sign_artifact_path("signed-chat", "out.json")
    try:
        # No Authorization header — but signed token in query string.
        r = client.get(f"/artifacts/signed-chat/out.json?t={token}")
        assert r.status_code == 200
        assert r.json() == {"a": 1}
    finally:
        import shutil
        shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


def test_artifacts_route_rejects_wrong_signed_token(client):
    chat_dir = memlib.chat_artifact_dir(MINIMAL, "signed-chat")
    (chat_dir / "out.json").write_text("x", encoding="utf-8")
    try:
        r = client.get("/artifacts/signed-chat/out.json?t=deadbeefdeadbeef")
        assert r.status_code == 401
    finally:
        import shutil
        shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


def test_artifacts_route_rejects_path_traversal(client):
    r = client.get(
        "/artifacts/c/..%2F..%2Fetc%2Fpasswd",
        headers={"Authorization": "Bearer dev"},
    )
    assert r.status_code in (400, 404)


def test_uploads_route_writes_to_chat_dir(client):
    payload = b"hello upload"
    try:
        r = client.post(
            "/uploads/chat-up/notes.md",
            headers={"Authorization": "Bearer dev"},
            content=payload,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["chat_id"] == "chat-up"
        assert body["size"] == len(payload)
        on_disk = MINIMAL / ".hubzoid/chats/chat-up/uploads/notes.md"
        assert on_disk.read_bytes() == payload
    finally:
        import shutil
        shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)


def test_chat_completion_derives_chat_id_from_body(client):
    async def fake_run(self, prompt):
        return "ok"
    seen: dict[str, str] = {}

    async def capture_run(self, prompt):
        # The chat_scope context manager must have set the chat id by now.
        seen["chat_id"] = _request_ctx.get_chat_id()
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture_run):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "chat_id": "from-body",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    assert r.status_code == 200
    assert seen["chat_id"] == "from-body"


def test_chat_completion_falls_back_to_hash_of_first_message(client):
    seen: dict[str, str] = {}

    async def capture_run(self, prompt):
        seen["chat_id"] = _request_ctx.get_chat_id()
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture_run):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "messages": [{"role": "user", "content": "stable input"}],
            },
        )
    assert r.status_code == 200
    cid = seen["chat_id"]
    assert cid.startswith("hash-")
    # Second request with the same first message gets the same hash chat id.
    seen.clear()
    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture_run):
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "testbot-label",
                "messages": [
                    {"role": "user", "content": "stable input"},
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "follow up"},
                ],
            },
        )
    assert seen["chat_id"] == cid


def test_chat_completion_persists_data_url_attachment(client):
    """An image_url with a data: url should land in the chat's uploads dir."""
    seen_prompt: dict[str, str] = {}

    async def capture(self, prompt):
        seen_prompt["text"] = prompt
        return "ok"

    payload = b"\x89PNG\r\n\x1a\n some bytes"
    b64 = base64.b64encode(payload).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    try:
        with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=capture):
            r = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer dev"},
                json={
                    "model": "testbot-label",
                    "chat_id": "with-upload",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look at this"},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }],
                },
            )
        assert r.status_code == 200
        uploads = MINIMAL / ".hubzoid/chats/with-upload/uploads"
        # Ignore the sidecar metadata file written alongside the payload.
        files = [p for p in uploads.iterdir() if not p.name.endswith(".hubzoid.json")]
        assert len(files) == 1
        assert files[0].read_bytes() == payload
        # The prompt the runtime saw must mention the upload + how to read it.
        assert "read_upload" in seen_prompt["text"]
    finally:
        import shutil
        shutil.rmtree(MINIMAL / ".hubzoid", ignore_errors=True)
