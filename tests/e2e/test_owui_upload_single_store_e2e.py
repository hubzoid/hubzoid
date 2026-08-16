"""End-to-end: an Open WebUI upload must land in the ONE canonical per-chat
uploads store, driven through the real bridge against the real HubzoidTestHub.

This is the regression guard for the "upload not read" bug: OWUI stored files
under `.openwebui-data/uploads/<id>_<name>` while `read_upload` / vision / ticket
attachments only ever looked in `.hubzoid/chats/<chat_id>/uploads/`. The bridge
now normalizes OWUI uploads into that single canonical store at ingest, so every
reader resolves them from one place and no downstream tool has to know OWUI
exists.

The model is stubbed (we assert on what the bridge hands the runtime + what lands
on disk), so this spends no tokens. It runs the full HTTP ingest path: auth,
chat-id derivation, `_normalize_owui_uploads`, chat_scope. Skips when the test
hub isn't checked out beside the repo.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# HubzoidTestHub lives beside the HubZoid repo: <...>/Hubzoid/HubzoidTestHub/test-hub
TEST_HUB = Path(__file__).resolve().parents[3] / "HubzoidTestHub" / "test-hub"

pytestmark = pytest.mark.skipif(
    not TEST_HUB.is_dir(),
    reason=f"HubzoidTestHub not present at {TEST_HUB}",
)

_E2E_PREFIX = "e2e-single-store"


def _make_owui_prompt(refs: list[tuple[str, str]], user_query: str) -> str:
    """A realistic OWUI RAG-wrapped prompt. refs: [(file_id, name), ...]."""
    sources = "\n".join(
        f'<source id="{i+1}" name="{name}" resource-type="file" '
        f'resource-id="{fid}">chunk content</source>'
        for i, (fid, name) in enumerate(refs)
    )
    return (
        "### Task:\nRespond to the user query using the provided context.\n\n"
        "<context>\n" + sources + "\n</context>\n\n" + user_query
    )


def _canonical(chat_id: str, name: str) -> Path:
    return TEST_HUB / ".hubzoid" / "chats" / chat_id / "uploads" / name


@pytest.fixture(autouse=True)
def _cleanup():
    """Remove only the artifacts this suite stages, before and after, so the
    real test hub's own chat state is never touched."""
    def _purge():
        chats = TEST_HUB / ".hubzoid" / "chats"
        for d in chats.glob(f"{_E2E_PREFIX}*"):
            shutil.rmtree(d, ignore_errors=True)
        for f in (TEST_HUB / ".openwebui-data" / "uploads").glob(f"{_E2E_PREFIX}*"):
            f.unlink(missing_ok=True)
    _purge()
    yield
    _purge()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(TEST_HUB))
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid.server import build_app
    return TestClient(build_app())


def _stage_owui_file(name: str, payload: bytes) -> str:
    """Write a file where OWUI would store it and return the file_id used."""
    file_id = f"{_E2E_PREFIX}-{name}"
    owui = TEST_HUB / ".openwebui-data" / "uploads"
    owui.mkdir(parents=True, exist_ok=True)
    (owui / f"{file_id}_{name}").write_bytes(payload)
    return file_id


def _post(client, chat_id: str, prompt: str, captured: dict):
    async def fake_run(self, prompt):
        captured["prompt"] = prompt
        return "ok (stubbed — no model call)"

    from unittest.mock import patch
    with patch("hubzoid.factory_claude.ClaudeRuntime.run", new=fake_run):
        return client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={
                "model": "test",
                "chat_id": chat_id,
                "messages": [{"role": "user", "content": prompt}],
            },
        )


def test_owui_document_lands_in_single_store(client):
    name = "e2e_spec.json"
    body = b'{"e2e": "single-store", "n": 42}'
    file_id = _stage_owui_file(name, body)
    chat_id = f"{_E2E_PREFIX}-doc"
    captured: dict = {}

    r = _post(client, chat_id, _make_owui_prompt([(file_id, name)], "Summarize this."), captured)
    assert r.status_code == 200, r.text

    # The file is now in THE canonical per-chat store — where read_upload looks.
    copied = _canonical(chat_id, name)
    assert copied.is_file(), "OWUI upload was not normalized into the canonical uploads store"
    assert copied.read_bytes() == body

    # The agent is pointed at read_upload — never at the raw OWUI path or RAG wrapper.
    prompt = captured["prompt"]
    assert f"read_upload('{name}')" in prompt
    assert ".openwebui-data" not in prompt
    assert "<source" not in prompt and "<context>" not in prompt
    # The canonical on-disk path is advertised so path-accepting tools / scripts
    # (the IRS test_template.py <file> flow) keep working — no IRS-side change needed.
    assert str(copied) in prompt
    assert prompt.rstrip().endswith("Summarize this.")


def test_owui_image_gets_vision_marker(client):
    name = "e2e_shot.png"
    file_id = _stage_owui_file(name, b"\x89PNG\r\n\x1a\n\x00e2e")
    chat_id = f"{_E2E_PREFIX}-img"
    captured: dict = {}

    r = _post(client, chat_id, _make_owui_prompt([(file_id, name)], "What is in this?"), captured)
    assert r.status_code == 200, r.text

    assert _canonical(chat_id, name).is_file()
    # Images ride the vision channel: vision_inject expands `[Image: name]` into a
    # real image block at model-call time — so it must NOT be a read_upload note.
    prompt = captured["prompt"]
    assert f"[Image: {name}]" in prompt
    assert "read_upload" not in prompt


def test_unresolved_owui_file_is_loud_not_silent(client):
    """The exact failure that burned IRS: a referenced file whose bytes are gone
    must surface a visible note the agent relays — never a silent drop that makes
    a user paste the whole file into chat."""
    name = "e2e_missing.json"
    file_id = f"{_E2E_PREFIX}-gone"  # deliberately NOT staged on disk
    chat_id = f"{_E2E_PREFIX}-loud"
    captured: dict = {}

    r = _post(client, chat_id, _make_owui_prompt([(file_id, name)], "Read the file."), captured)
    assert r.status_code == 200, r.text

    prompt = captured["prompt"]
    assert name in prompt
    assert "could not be read" in prompt.lower()
    assert prompt.rstrip().endswith("Read the file.")
