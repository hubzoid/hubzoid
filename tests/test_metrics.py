"""Tests for #5: token/cost capture, JSONL ledger, usage envelope, /metrics.

  * metrics.py         — record_interaction (append, skip-zero), read, summary
  * _request_ctx       — record_usage / drain_usage sink
  * server.py          — blocking usage envelope, streaming usage chunk, and
                         the /metrics endpoint, exercised via TestClient with a
                         stubbed runtime that reports usage.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hubzoid import metrics as metrics_lib
from hubzoid import _request_ctx
from hubzoid.access import Identity

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# ---------------------------------------------------------------------------
# ledger unit
# ---------------------------------------------------------------------------
def _ident(user="priya", surface="owui"):
    return Identity.make(user=user, groups=[], surface=surface)


def test_record_appends_and_summary_aggregates(tmp_path):
    metrics_lib.record_interaction(tmp_path, chat_id="c1", identity=_ident("priya"),
                                   model="claude-local",
                                   usage={"input_tokens": 100, "output_tokens": 20,
                                          "cost_usd": None, "num_turns": 2})
    metrics_lib.record_interaction(tmp_path, chat_id="c2", identity=_ident("ravi"),
                                   model="gpt-4o",
                                   usage={"input_tokens": 50, "output_tokens": 10,
                                          "cost_usd": 0.004})
    entries = metrics_lib.read_entries(tmp_path)
    assert len(entries) == 2 and entries[0]["input_tokens"] == 100

    s = metrics_lib.summary(tmp_path)
    assert s["count"] == 2
    assert s["totals"]["total_tokens"] == 180
    assert s["totals"]["cost_usd"] == pytest.approx(0.004)
    assert s["by_model"]["claude-local"]["total_tokens"] == 120
    assert s["by_user"]["ravi"]["output_tokens"] == 10
    assert set(s["by_day"].keys())  # keyed by date


def test_record_skips_zero_token_interactions(tmp_path):
    metrics_lib.record_interaction(tmp_path, chat_id="c", identity=_ident(),
                                   model="m", usage={})
    metrics_lib.record_interaction(tmp_path, chat_id="c", identity=_ident(),
                                   model="m", usage={"input_tokens": 0, "output_tokens": 0})
    assert metrics_lib.read_entries(tmp_path) == []


def test_summary_empty_is_zeroed(tmp_path):
    s = metrics_lib.summary(tmp_path)
    assert s["count"] == 0 and s["totals"]["total_tokens"] == 0


def test_iter_entries_streams(tmp_path):
    for i in range(3):
        metrics_lib.record_interaction(tmp_path, chat_id="c", identity=_ident(),
                                       model="m", usage={"input_tokens": 1, "output_tokens": 1})
    it = metrics_lib.iter_entries(tmp_path)
    assert hasattr(it, "__next__")           # a generator, not a list
    assert sum(1 for _ in it) == 3


def test_summary_survives_tampered_numeric_field(tmp_path):
    # a hand-tampered but valid-JSON line must not 500 /metrics (via summary)
    p = metrics_lib.ledger_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"input_tokens":"abc","output_tokens":5,"total_tokens":5,"model":"m"}\n')
    s = metrics_lib.summary(tmp_path)         # must not raise
    assert s["count"] == 1 and s["totals"]["output_tokens"] == 5


def test_record_never_raises_on_bad_usage_values(tmp_path):
    # non-numeric token values must not raise (F1)
    metrics_lib.record_interaction(tmp_path, chat_id="c", identity=_ident(),
                                   model="m",
                                   usage={"input_tokens": "abc", "output_tokens": None})
    # coerced to 0/0 -> skipped as zero, no exception, no file
    assert metrics_lib.read_entries(tmp_path) == []
    # a valid one still records
    metrics_lib.record_interaction(tmp_path, chat_id="c", identity=_ident(),
                                   model="m",
                                   usage={"input_tokens": "3", "output_tokens": 2})
    assert metrics_lib.read_entries(tmp_path)[0]["input_tokens"] == 3


def test_claude_usage_sums_cache_tokens():
    from hubzoid.factory_claude import _record_claude_usage
    from types import SimpleNamespace

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


def test_read_entries_skips_corrupt_lines(tmp_path):
    p = metrics_lib.ledger_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}\n'
                 'NOT JSON\n'
                 '{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}\n')
    assert len(metrics_lib.read_entries(tmp_path)) == 2


# ---------------------------------------------------------------------------
# request-context usage sink
# ---------------------------------------------------------------------------
def test_usage_sink_record_and_drain():
    with _request_ctx.chat_scope("c1"):
        _request_ctx.record_usage({"input_tokens": 7, "output_tokens": 3})
        drained = _request_ctx.drain_usage()
        assert drained["input_tokens"] == 7 and drained["output_tokens"] == 3
        # second drain is empty (cleared)
        assert _request_ctx.drain_usage() == {}


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


def test_blocking_envelope_has_usage_and_writes_ledger(hub_client):
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
    usage = r.json()["usage"]
    assert usage == {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}
    # ledger recorded
    entries = metrics_lib.read_entries(hub)
    assert len(entries) == 1 and entries[0]["model"] == "testbot-label"


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
    # find the usage chunk (choices == [] and a usage field)
    usage_seen = None
    for line in r.text.splitlines():
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        obj = json.loads(line[len("data: "):])
        if obj.get("usage") and obj.get("choices") == []:
            usage_seen = obj["usage"]
    assert usage_seen == {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}


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
    # opaque origin (the real protection) stays on; interactive caps allowed
    assert "sandbox" in csp and "allow-same-origin" not in csp
    assert "allow-scripts" in csp and "allow-forms" in csp


def test_metrics_endpoint_requires_auth_and_returns_summary(hub_client):
    hub, client = hub_client
    assert client.get("/metrics").status_code == 401

    async def fake_run(self, _prompt):
        _request_ctx.record_usage({"input_tokens": 5, "output_tokens": 5})
        return "ok"

    with patch("hubzoid.runtime.OpenAIAgentsRuntime.run", new=fake_run):
        client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer dev"},
                    json={"model": "testbot-label",
                          "messages": [{"role": "user", "content": "x"}]})
    r = client.get("/metrics", headers={"Authorization": "Bearer dev"})
    assert r.status_code == 200
    s = r.json()
    assert s["count"] == 1 and s["totals"]["total_tokens"] == 10
