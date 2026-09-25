"""The OpenAI Agents SDK exports runs (prompts, tool data) to OpenAI's trace
dashboard whenever an OpenAI key is present. Hubzoid keeps that off unless the
hub sets HUBZOID_OPENAI_TRACING=true. Checked at the HTTP layer: no request to
the trace ingest endpoint leaves the process by default.
"""
from __future__ import annotations

import httpx
import pytest
from agents import custom_span, set_tracing_disabled, trace
from agents.tracing import get_trace_provider

from hubzoid import runtime as runtime_lib


@pytest.fixture
def trace_posts(monkeypatch, tmp_path):
    """Build an OpenAI-backend hub with a key set; record trace ingest POSTs."""
    posts: list[str] = []

    def fake_post(self, url, *a, **k):
        posts.append(str(url))
        return httpx.Response(200, request=httpx.Request("POST", str(url)))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("MODEL", "openai/gpt-4o-mini")
    monkeypatch.delenv("HUBZOID_OPENAI_TRACING", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_DISABLE_TRACING", raising=False)
    (tmp_path / "AGENTS.md").write_text("---\nname: t\ndescription: d\n---\nbody")
    yield tmp_path, posts
    set_tracing_disabled(True)  # never leave SDK tracing on for later tests


def _emit_trace():
    with trace("probe"):
        with custom_span("step", data={"prompt": "private text"}):
            pass
    get_trace_provider().force_flush()


def _ingest(posts):
    return [u for u in posts if "/traces" in u]


def test_sdk_tracing_is_off_by_default(trace_posts):
    hub, posts = trace_posts
    runtime_lib.build(hub)
    _emit_trace()
    assert _ingest(posts) == []


def test_sdk_tracing_can_be_enabled(trace_posts, monkeypatch):
    hub, posts = trace_posts
    monkeypatch.setenv("HUBZOID_OPENAI_TRACING", "true")
    runtime_lib.build(hub)
    _emit_trace()
    assert _ingest(posts), "opt-in should export to the OpenAI trace endpoint"
