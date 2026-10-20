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
