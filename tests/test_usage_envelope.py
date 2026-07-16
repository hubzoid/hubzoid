"""Usage-envelope tests (the ledger is retired; cost/usage now via OTel).

What survives the ledger deletion and still needs guarding:
  * _record_claude_usage / _record_openai_usage -> _request_ctx sink
  * server.py — blocking usage envelope + streaming usage chunk that feed Open
    WebUI's native per-message token column (no backend required)
  * /metrics is gone (retired with the JSONL ledger)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hubzoid import _request_ctx

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# ---------------------------------------------------------------------------
# usage capture -> request-context sink (feeds the envelope)
# ---------------------------------------------------------------------------
def test_claude_usage_sums_cache_tokens():
    from types import SimpleNamespace

    from hubzoid.factory_claude import _record_claude_usage

    msg = SimpleNamespace(
        usage={"input_tokens": 10, "cache_read_input_tokens": 90,
               "cache_creation_input_tokens": 5, "output_tokens": 20},
        total_cost_usd=None, num_turns=1,
    )
    with _request_ctx.chat_scope("c"):
        _record_claude_usage(msg)
        u = _request_ctx.drain_usage()
    assert u["input_tokens"] == 105        # 10 + 90 + 5, not just 10
    assert u["output_tokens"] == 20 and u["total_tokens"] == 125


def test_usage_sink_record_and_drain():
    with _request_ctx.chat_scope("c1"):
        _request_ctx.record_usage({"input_tokens": 7, "output_tokens": 3})
        drained = _request_ctx.drain_usage()
        assert drained["input_tokens"] == 7 and drained["output_tokens"] == 3
        assert _request_ctx.drain_usage() == {}   # cleared after drain


# ---------------------------------------------------------------------------
# server integration
# ---------------------------------------------------------------------------
@pytest.fixture
def hub_client(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub)
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL_LABEL", "testbot-label")
    from hubzoid.server import build_app
    return hub, TestClient(build_app())


def test_blocking_envelope_has_usage(hub_client):
    hub, client = hub_client

    async def fake_run(self, _prompt):
        _request_ctx.record_usage({"input_tokens": 40, "output_tokens": 12,
                                   "cost_usd": None, "num_turns": 1})
        return "pong"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=fake_run):
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer dev"},
                        json={"model": "testbot-label",
                              "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200
    assert r.json()["usage"] == {
        "prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52,
    }


def test_streaming_emits_usage_chunk(hub_client):
    hub, client = hub_client

    async def fake_stream(self, _prompt):
        _request_ctx.record_usage({"input_tokens": 8, "output_tokens": 4})
        yield "hel"
        yield "lo"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.stream", new=fake_stream):
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": "Bearer dev"},
                        json={"model": "testbot-label", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    usage_seen = None
    for line in r.text.splitlines():
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        obj = json.loads(line[len("data: "):])
        if obj.get("usage") and obj.get("choices") == []:
            usage_seen = obj["usage"]
    assert usage_seen == {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}


def test_metrics_endpoint_is_gone(hub_client):
    # The JSONL ledger and its /metrics endpoint are retired. Cost/usage lives
    # in OTel now; the route must no longer exist.
    _, client = hub_client
    assert client.get("/metrics", headers={"Authorization": "Bearer dev"}).status_code == 404


def test_inline_artifact_is_sandboxed_by_csp(hub_client):
    """Agent-authored HTML artifacts are served with a CSP sandbox so embedded
    scripts can't reach Open WebUI's same-origin session."""
    from hubzoid import _signing, memory as memlib

    hub, client = hub_client
    adir = memlib.chat_artifact_dir(hub, "c1")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "dash.html").write_text("<h1>hi</h1>")
    token = _signing.sign_artifact_path("c1", "dash.html")
    r = client.get(f"/artifacts/c1/dash.html?t={token}")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "sandbox" in csp and "allow-same-origin" not in csp
    assert "allow-scripts" in csp and "allow-forms" in csp
