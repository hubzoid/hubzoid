"""One real model turn per runtime calls a personal MCP tool as the caller.

Explicit opt-in: set ``HUBZOID_TEST_CONNECT_E2E=1``. Each runtime then also
needs its own credentials, or it skips:

  * Claude: the ``claude`` CLI on PATH, logged in (``MODEL=claude-local``).
  * OpenAI Agents: ``OPENAI_API_KEY`` (or ``OPENROUTER_API_KEY``), model from
    ``HUBZOID_E2E_OPENAI_MODEL`` (default ``openai/gpt-4o-mini``).
  * Codex: ``HUBZOID_TEST_CODEX=1`` with an authenticated, supported Codex CLI.

The personal MCP server is an in-process HTTP server that answers only the
caller's bearer, stored the way Open WebUI stores a connected token. Real
Google consent, a real Composio link and real WhatsApp delivery are manual
checks (see docs/mcp.md).
"""
from __future__ import annotations

import os
import shutil

import pytest

from hubzoid import _request_ctx, runtime
from hubzoid.access import Identity, identity_scope
from tests import connect_helpers as h

pytestmark = pytest.mark.e2e

X = "x@example.org"
SECRET = "e2e-secret"
PHRASE = "violet heron at dawn"


@pytest.fixture(autouse=True)
def _opt_in():
    if os.environ.get("HUBZOID_TEST_CONNECT_E2E") != "1":
        pytest.skip("set HUBZOID_TEST_CONNECT_E2E=1 to run real-model connection checks")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    server = h.mcp_server("mail", {"tok-x": X},
                          {"mailbox_phrase": lambda who: f"The mailbox phrase for {who} is {PHRASE}."})
    url = server.__enter__()
    hub = tmp_path / "e2ehub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text(
        "---\nname: Connect e2e\n---\nWhen asked for the mailbox phrase, call the mailbox_phrase "
        "tool and repeat its answer exactly.\n")
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ux", X)], secret=SECRET,
                servers=[{"id": "mail", "name": "Mail", "url": url}])
    h.connect(db, user_id="ux", server_id="mail", secret=SECRET, access_token="tok-x")
    h.owui_env(monkeypatch, db, SECRET)
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_BROWSER", "off")
    try:
        yield hub
    finally:
        server.__exit__(None, None, None)


async def _turn(hub) -> str:
    rt = runtime.build(hub)
    await rt.aopen()
    try:
        with identity_scope(Identity.make(user=X, surface="owui")), _request_ctx.chat_scope("e2e"):
            out = await rt.run("What is my mailbox phrase? Use the tool.")
        assert getattr(rt, "last_error", None) is None, out
        return out
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_claude_calls_the_personal_tool(hub, monkeypatch):
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")
    monkeypatch.setenv("MODEL", "claude-local")
    assert PHRASE in (await _turn(hub)).lower()


@pytest.mark.asyncio
async def test_openai_calls_the_personal_tool(hub, monkeypatch):
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        pytest.skip("no OpenAI or OpenRouter key")
    monkeypatch.setenv("MODEL", os.environ.get("HUBZOID_E2E_OPENAI_MODEL", "openai/gpt-4o-mini"))
    assert PHRASE in (await _turn(hub)).lower()


@pytest.mark.asyncio
async def test_codex_calls_the_personal_tool(hub, monkeypatch):
    from hubzoid.factory_codex import codex_available

    if os.environ.get("HUBZOID_TEST_CODEX") != "1" or not codex_available():
        pytest.skip("set HUBZOID_TEST_CODEX=1 with an authenticated, supported Codex CLI")
    monkeypatch.setenv("MODEL", "codex-local")
    assert PHRASE in (await _turn(hub)).lower()
