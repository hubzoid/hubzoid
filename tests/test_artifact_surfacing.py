"""Deterministic, model-independent download-link surfacing.

A download link reaches the user only when it ends up in the assistant's
reply. We do not want to depend on the model copying the `write_artifact`
result through (Claude does this reliably; some OpenAI/Azure models do not).

So `write_artifact` records every link it produces in a per-request
registry (`_request_ctx`), and both runtimes drain that registry at the
end of the turn and append any link the model did not already echo
(`tool_events.format_artifact_footer`). The link then appears regardless
of backend or model, on every surface (Open WebUI bridge, Slack adapter)
because both go through the same runtime.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hubzoid import _request_ctx
from hubzoid import tool_events
from hubzoid.tools import files as files_mod


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_chat_artifacts.py style)
# ---------------------------------------------------------------------------
def _call(tool, **kwargs: Any) -> str:
    from agents.tool_context import ToolContext

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
    session_id: str = "sess"
    settings: Any = None


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    hub = tmp_path / "hub"
    hub.mkdir()
    out = hub / "output" / "sess"
    out.mkdir(parents=True)
    return _Ctx(hub_dir=hub, output_dir=out)


@pytest.fixture(autouse=True)
def _reset_request_ctx():
    _request_ctx.set_chat_id(None)
    yield
    _request_ctx.set_chat_id(None)


# ---------------------------------------------------------------------------
# Registry: record / drain / per-request isolation
# ---------------------------------------------------------------------------
def test_record_and_drain_returns_artifacts():
    with _request_ctx.chat_scope("a"):
        _request_ctx.record_artifact("x.json", "http://u/x.json?t=1")
        out = _request_ctx.drain_artifacts()
    assert out == [{"name": "x.json", "url": "http://u/x.json?t=1"}]


def test_drain_clears_registry():
    with _request_ctx.chat_scope("a"):
        _request_ctx.record_artifact("x", "http://u/x")
        _request_ctx.drain_artifacts()
        # A second drain in the same turn yields nothing.
        assert _request_ctx.drain_artifacts() == []


def test_chat_scope_isolates_artifacts_between_requests():
    with _request_ctx.chat_scope("a"):
        _request_ctx.record_artifact("x", "http://u/x")
    # A new request starts clean — no leak from the previous turn.
    with _request_ctx.chat_scope("b"):
        assert _request_ctx.drain_artifacts() == []


# ---------------------------------------------------------------------------
# write_artifact records the link it produced
# ---------------------------------------------------------------------------
def test_write_artifact_records_link_for_surfacing(ctx, monkeypatch):
    monkeypatch.setenv("BRIDGE_PORT", "9999")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    write = _by_name(files_mod.make(ctx), "write_artifact")
    with _request_ctx.chat_scope("chat-rec"):
        _call(write, filename="form.json", content="{}")
        drained = _request_ctx.drain_artifacts()
    assert len(drained) == 1
    assert drained[0]["name"] == "form.json"
    assert "artifacts/chat-rec/form.json?t=" in drained[0]["url"]


def test_write_artifact_without_chat_records_nothing(ctx):
    write = _by_name(files_mod.make(ctx), "write_artifact")
    # No chat in scope -> no download URL -> nothing to surface.
    _call(write, filename="r.txt", content="hi")
    assert _request_ctx.drain_artifacts() == []


# ---------------------------------------------------------------------------
# surface_artifact: link a file written by something other than write_artifact
# (e.g. a validator emitting an encoded sidecar into the same artifacts dir)
# ---------------------------------------------------------------------------
def test_surface_artifact_records_link_in_chat(ctx, monkeypatch):
    monkeypatch.setenv("BRIDGE_PORT", "8001")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    with _request_ctx.chat_scope("chat-s"):
        ok = files_mod.surface_artifact("form.json.encoded.txt")
        drained = _request_ctx.drain_artifacts()
    assert ok is True
    assert len(drained) == 1
    assert drained[0]["name"] == "form.json.encoded.txt"
    assert "artifacts/chat-s/form.json.encoded.txt?t=" in drained[0]["url"]


def test_surface_artifact_without_chat_returns_false(ctx):
    assert files_mod.surface_artifact("x.txt") is False
    assert _request_ctx.drain_artifacts() == []


# ---------------------------------------------------------------------------
# Formatter: surface unechoed links, dedup echoed ones
# ---------------------------------------------------------------------------
def test_footer_surfaces_unechoed_link():
    url = "http://h/artifacts/c/form.json?t=ab"
    footer = tool_events.format_artifact_footer(
        [{"name": "form.json", "url": url}], shown_text="Here is your form."
    )
    assert f"[Download form.json]({url})" in footer


def test_footer_dedupes_link_already_in_text():
    url = "http://h/artifacts/c/form.json?t=ab"
    footer = tool_events.format_artifact_footer(
        [{"name": "form.json", "url": url}],
        shown_text=f"Done: [Download form.json]({url})",
    )
    assert footer == ""


def test_footer_empty_when_no_artifacts():
    assert tool_events.format_artifact_footer([], shown_text="x") == ""


def test_footer_dedupes_repeated_link_from_multiple_write_calls():
    # write_artifact called twice for the same file (e.g. a revision after a
    # validation error) records the same URL twice — surface it only once.
    url = "http://h/artifacts/c/form.json?t=ab"
    enc = "http://h/artifacts/c/form.json.encoded.txt?t=cd"
    footer = tool_events.format_artifact_footer(
        [
            {"name": "form.json", "url": url},
            {"name": "form.json", "url": url},
            {"name": "form.json.encoded.txt", "url": enc},
        ],
        shown_text="",
    )
    assert footer.count(f"[Download form.json]({url})") == 1
    assert footer.count(f"[Download form.json.encoded.txt]({enc})") == 1


# ---------------------------------------------------------------------------
# Runtime wiring: the link is surfaced even when the model never echoes it
# ---------------------------------------------------------------------------
class _FakeStreamResult:
    """Stand-in for Runner.run_streamed result with no events (model said
    nothing). The runtime should still surface a recorded artifact."""

    def stream_events(self):
        async def _gen():
            return
            yield  # pragma: no cover — makes this an async generator

        return _gen()


def test_openai_runtime_surfaces_recorded_artifact(monkeypatch):
    import agents

    from hubzoid.runtime import OpenAIAgentsRuntime

    monkeypatch.setattr(
        agents.Runner,
        "run_streamed",
        lambda agent, prompt, max_turns=None: _FakeStreamResult(),
    )
    rt = OpenAIAgentsRuntime(SimpleNamespace(name="t", mcp_servers=[]))
    url = "http://127.0.0.1:8000/artifacts/c1/form.json?t=abc"

    async def _collect() -> str:
        out: list[str] = []
        with _request_ctx.chat_scope("c1"):
            _request_ctx.record_artifact("form.json", url)
            async for chunk in rt.stream("hi"):
                out.append(chunk)
        return "".join(out)

    text = asyncio.run(_collect())
    assert url in text
    assert "Download form.json" in text


def test_claude_runtime_surfaces_recorded_artifact(monkeypatch):
    import claude_agent_sdk

    from hubzoid.factory_claude import ClaudeRuntime

    def _fake_query(*, prompt, options):
        async def _gen():
            return
            yield  # pragma: no cover — async generator with no messages

        return _gen()

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    rt = ClaudeRuntime(name="x", options=object())
    url = "http://127.0.0.1:8000/artifacts/c2/flow.json?t=zz"

    async def _collect() -> str:
        out: list[str] = []
        with _request_ctx.chat_scope("c2"):
            _request_ctx.record_artifact("flow.json", url)
            async for chunk in rt.stream("hi"):
                out.append(chunk)
        return "".join(out)

    text = asyncio.run(_collect())
    assert url in text
    assert "Download flow.json" in text
