"""Bridge tests using FastAPI TestClient. No real LLM calls (we mock the runtime)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev,alt")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    yield


@pytest.fixture
def client():
    from hubzoid.server import build_app
    return TestClient(build_app())


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["agent"] == "testbot"


def test_models_requires_auth(client):
    assert client.get("/v1/models").status_code == 401


def test_models_returns_label(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer dev"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"][0]["id"] == "testbot-label"


def test_chat_empty_messages_400(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer dev"},
        json={"messages": []},
    )
    assert r.status_code == 400


def test_chat_blocking_returns_assistant_message(client):
    """Patch the runtime's run() with an async stub that returns 'pong'."""

    async def fake_run(self, _prompt):
        return "pong"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=fake_run):
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer dev"},
            json={"model": "testbot-label", "messages": [{"role": "user", "content": "ping"}]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["choices"][0]["finish_reason"] == "stop"
