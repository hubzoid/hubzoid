"""A person's API key on the bridge: the chat API only, in their own chat
space. Files, uploads and artifacts take the bridge's own keys."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns(".hubzoid"))
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid.auth import AuthUser, api_keys, users
    from hubzoid.server import build_app

    ana = users.create(hub, email="ana@example.com", name="Ana", password="pw-Very-long-1")
    _, key = api_keys.create(hub, AuthUser(id=ana["id"], email=ana["email"], name="Ana",
                                           role="user"), name="s", operations=["chat", "read", "manage"])
    return TestClient(build_app()), key


def test_personal_keys_never_reach_bridge_files(bridge):
    client, key = bridge
    bearer = {"authorization": f"Bearer {key}"}
    assert client.post("/uploads/c1/x.txt", content=b"hi", headers=bearer).status_code == 401
    assert client.get("/artifacts/c1/x.txt", headers=bearer).status_code in (401, 404)
    assert client.post("/uploads/c1/x.txt", content=b"hi",
                       headers={"authorization": "Bearer dev"}).status_code == 200


def test_personal_chat_ids_are_per_account():
    from hubzoid.server import _personal_chat_id

    assert _personal_chat_id("u_ana", "c1") == _personal_chat_id("u_ana", "c1")
    assert _personal_chat_id("u_ana", "c1") != _personal_chat_id("u_ben", "c1")
    assert _personal_chat_id("u_ana", "c1").startswith("key-")


def test_a_key_asking_for_another_agent_is_told_where_it_is(bridge):
    """Behind a gateway the bare /v1 is the first agent: a model name for
    another agent is refused with its API base, never answered by this one."""
    client, key = bridge
    r = client.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"},
                    json={"model": "projects-hub", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404 and "/b/<agent>/v1" in r.json()["detail"]["message"]


def test_a_malformed_key_never_falls_back_to_another_sign_in(bridge):
    """An overlong or broken hzp_ value is refused, not answered as the
    cookie or the local owner (Codex review)."""
    client, _ = bridge
    bad = {"authorization": "Bearer hzp_" + "a" * 300}
    assert client.get("/v1/models", headers=bad).status_code == 401


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    """The bridge on the scripted test runtime: "tool" in a message makes one
    real tool call, so its display block would appear in the text."""
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns(".hubzoid"))
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid.auth import AuthUser, api_keys, users
    from hubzoid.server import build_app

    from hubzoid.access import store_for

    ana = users.create(hub, email="ana@example.com", name="Ana", password="pw-Very-long-1")
    store_for(hub).grant("ana@example.com", hub.name, "use_hub")
    _, key = api_keys.create(hub, AuthUser(id=ana["id"], email=ana["email"], name="Ana",
                                           role="user"), name="s", operations=["chat"])
    return TestClient(build_app()), key


def _ask(client, key, *, stream):
    return client.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"},
                       json={"stream": stream, "messages": [{"role": "user", "content": "use a tool please"}]})


def test_an_api_client_gets_the_answer_without_tool_markup(scripted):
    """HUB-140 F3: no <details type="tool_calls"> in /v1 text for a personal key."""
    import json

    client, key = scripted
    plain = _ask(client, key, stream=False).json()["choices"][0]["message"]["content"]
    assert plain and "<details" not in plain and not plain.startswith("\n")
    r = _ask(client, key, stream=True)
    chunks = [json.loads(line[6:]) for line in r.text.splitlines()
              if line.startswith("data: {")]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text.strip() == plain.strip()
    # HUB-140 F27: one id and created time for every chunk of the answer,
    # status and usage chunks included.
    assert len({c["id"] for c in chunks}) == 1 and len({c["created"] for c in chunks}) == 1


def test_internal_surfaces_keep_the_tool_markup():
    """Slack, WhatsApp and Telegram call with the bridge key and strip the
    markup themselves (Slack also reads the thinking marker)."""
    import asyncio
    import json

    from hubzoid.server import _stream

    class Rt:
        async def stream(self, prompt):
            yield '<details type="tool_calls" name="x">\n</details>\n'
            yield "Answer."

    async def collect(plain):
        out = [c async for c in _stream(Rt(), "p", "m", "c1", plain=plain)]
        chunks = [json.loads(b.decode()[6:]) for b in out if b.startswith(b"data: {")]
        return "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])

    assert "<details" in asyncio.run(collect(False))
    assert asyncio.run(collect(True)) == "Answer."
