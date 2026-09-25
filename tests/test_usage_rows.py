"""Every chat turn through the bridge writes one usage row to Hubzoid's own
database, whichever chat UI or channel sent it, with no message content."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from hubzoid import _request_ctx


@pytest.fixture
def client(tmp_path, monkeypatch):
    hub = tmp_path / "sales"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: sales\ndescription: d\n---\nbody")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_API_KEYS", "k-usage")
    monkeypatch.delenv("HUBZOID_OPERATIONAL_DB", raising=False)
    from hubzoid.server import build_app
    return TestClient(build_app()), hub


def _rows(hub):
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    with eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_usage"))]


async def _answer(self, prompt):
    _request_ctx.record_usage({"input_tokens": 1200, "output_tokens": 80,
                               "model": "openrouter/anthropic/claude-haiku-4.5", "status": "ok"})
    return "Payments owns it."


async def _stream_answer(self, prompt):
    yield "Payments "
    _request_ctx.record_usage({"input_tokens": 900, "output_tokens": 40,
                               "model": "openrouter/anthropic/claude-haiku-4.5", "status": "ok"})
    yield "owns it."


def test_chat_turn_writes_a_usage_row(client):
    c, hub = client
    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=_answer):
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer k-usage",
                            "X-OpenWebUI-User-Email": "ann@example.org"},
                   json={"model": "sales", "chat_id": "c-1",
                         "messages": [{"role": "user", "content": "who owns checkout?"}]})
    assert r.status_code == 200
    (row,) = _rows(hub)
    assert row["hub"] == "sales" and row["surface"] == "web" and row["kind"] == "chat"
    assert row["subject"] == "ann@example.org" and row["chat_id"] == "c-1"
    assert (row["input_tokens"], row["output_tokens"], row["status"]) == (1200, 80, "ok")
    assert row["cost_usd"] == pytest.approx(1200 * 1e-6 + 80 * 5e-6)  # estimated from the price table
    assert "who owns checkout" not in str(row)  # no message content


def test_streamed_slack_turn_counts_as_slack(client):
    c, hub = client
    with patch("hubzoid.runtime.OpenAIAgentsRuntime.stream", new=_stream_answer):
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer k-usage", "X-Hubzoid-Surface": "slack",
                            "X-Hubzoid-User": "bo@example.org"},
                   json={"model": "sales", "stream": True, "chat_id": "s-1",
                         "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "[DONE]" in r.text
    (row,) = _rows(hub)
    assert (row["surface"], row["subject"], row["input_tokens"]) == ("slack", "bo@example.org", 900)
