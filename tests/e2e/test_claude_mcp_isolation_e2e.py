"""claude-local MCP isolation with the REAL `claude` CLI and synthetic local
MCP servers only (no personal Gmail, Drive or Slack is touched). Credentials
are synthetic and are checked to be absent from every process's arguments
while the connector is in use.

    pytest tests/e2e/test_claude_mcp_isolation_e2e.py -m e2e -v

One bounded check: two start-up tool listings (read before any model reply)
and one short model turn that calls two synthetic tools. It proves that the
host account's claude.ai connectors are absent, the hub's configured MCP server
and each person's own connector still connect and answer, each person gets
only their own connector, gated tools stay hidden from people without the
grant, and `hub.call_llm` has no tools at all. Self-skips without `claude`.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")]

SERVER = textwrap.dedent('''
    import sys
    from mcp.server.fastmcp import FastMCP

    mode = sys.argv[1]
    if mode == "stdio":
        mcp = FastMCP("hub-echo")

        @mcp.tool()
        def echo_marker(text: str) -> str:
            """Echo text back with a marker."""
            return "hub-echo:" + text

        mcp.run()
    else:
        import os, subprocess
        port, owner, seen = int(sys.argv[2]), sys.argv[3], sys.argv[4]
        token = os.environ["SYNTH_TOKEN"]    # from env, so it is in no process's argv
        mcp = FastMCP("notes")

        @mcp.tool()
        def whoami() -> str:
            """Name the person whose connector this is."""
            return "notes-for-" + owner

        app = mcp.streamable_http_app()

        async def guarded(scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope["headers"]).get(b"authorization", b"").decode()
                ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout
                with open(seen, "a") as f:
                    f.write(auth + "|argv_leak=" + str(token in ps) + "\\n")
                if auth != "Bearer " + token:
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
            await app(scope, receive, send)

        import uvicorn
        uvicorn.run(guarded, host="127.0.0.1", port=port, log_level="warning")
''')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"synthetic MCP server on {port} did not start")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from hubzoid import owui_mcp
    from hubzoid.access import store_for

    script = tmp_path / "synthetic_mcp.py"
    script.write_text(SERVER)
    hub = tmp_path / "iso-hub"
    (hub / "connectors").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: iso-hub\ndescription: d\n---\nUse the tools you are asked to use.\n")
    (hub / ".env").write_text("MODEL=claude-local/haiku\n")
    (hub / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "hub-echo": {"command": sys.executable, "args": [str(script), "stdio"]}}}))
    for k in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_BROWSER", "HUBZOID_RESTRICTED_SURFACES",
              "OPENROUTER_API_KEY", "JEV_OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    gs = store_for(hub)
    gs.set_authoritative(True, hub="iso-hub")
    gs.grant("ana@example.org", "iso-hub", "jev", actor="test")
    gs.grant("ben@example.org", "iso-hub", "use_hub", actor="test")

    procs, urls, seen = [], {}, {}
    for who in ("ana", "ben"):
        port = _free_port()
        seen[who] = tmp_path / f"seen-{who}.log"
        procs.append(subprocess.Popen([sys.executable, str(script), "http", str(port), who,
                                       str(seen[who])], env={**os.environ, "SYNTH_TOKEN": f"token-of-{who}"}))
        urls[who] = f"http://127.0.0.1:{port}/mcp"
    for url in urls.values():
        _wait_port(int(url.split(":")[2].split("/")[0]))

    def specs(hub_dir, ident, **_kw):   # stands in for the person's Open WebUI connection
        who = (ident.user or "").split("@")[0]
        if who not in urls:
            return {}, []
        return ({f"notes-{who}": {"type": "http", "url": urls[who],
                                  "headers": {"Authorization": f"Bearer token-of-{who}"}}},
                [f"mcp__notes-{who}__*"])

    monkeypatch.setattr(owui_mcp, "per_user_specs", specs)
    yield hub, seen
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


async def _run(options, prompt: str, *, init_only: bool) -> tuple[dict, str]:
    from claude_agent_sdk import AssistantMessage, SystemMessage, query
    from claude_agent_sdk.types import TextBlock

    init, text = {}, []
    gen = query(prompt=prompt, options=options)
    try:
        async for m in gen:
            if isinstance(m, SystemMessage) and m.subtype == "init":
                init = m.data
                if init_only:
                    break
            elif isinstance(m, AssistantMessage):
                text += [b.text for b in m.content if isinstance(b, TextBlock)]
    finally:
        await gen.aclose()
    return init, "".join(text)


def _servers(init) -> dict:
    return {s["name"]: s["status"] for s in init.get("mcp_servers", [])}


def _mcp_files() -> set:
    import tempfile
    from pathlib import Path

    return set(Path(tempfile.gettempdir()).glob("hubzoid-mcp-*.json"))


def test_strict_isolation_live(setup):
    from hubzoid.access import Identity, identity_scope
    from hubzoid.factory_claude import PrivateMcpConfig, build_claude_runtime, tool_free_options

    hub, seen = setup
    rt = build_claude_runtime(hub)
    files_before = _mcp_files()

    def start_up(who):
        with identity_scope(Identity.make(f"{who}@example.org", surface="owui")):
            with PrivateMcpConfig(rt._options_for_turn()) as cfg:
                return asyncio.run(_run(cfg.options, "Reply OK.", init_only=True))[0]

    ben, ana = start_up("ben"), start_up("ana")

    async def chat_turn():    # the real chat path: ClaudeRuntime.stream
        with identity_scope(Identity.make("ana@example.org", surface="owui")):
            return "".join([part async for part in rt.stream(
                "Call mcp__hub-echo__echo_marker with text strict-ok, then call "
                "mcp__notes-ana__whoami. Reply with only the two tool outputs, separated by a space.")])

    reply = asyncio.run(chat_turn())

    for init in (ana, ben):
        assert not [n for n in _servers(init) if "claude.ai" in n]
        assert not [t for t in init["tools"] if "claude_ai" in t]
        assert _servers(init)["hubzoid"] == "connected" and _servers(init)["hub-echo"] == "connected"
    assert _servers(ana)["notes-ana"] == "connected" and "notes-ben" not in _servers(ana)
    assert _servers(ben)["notes-ben"] == "connected" and "notes-ana" not in _servers(ben)
    assert "mcp__hubzoid__call_jev" in ana["tools"] and "mcp__hubzoid__call_jev" not in ben["tools"]
    assert "hub-echo:strict-ok" in reply and "notes-for-ana" in reply
    # each connector saw only its own person's token, and never while it was in any argv
    assert set(seen["ana"].read_text().split("\n")) - {""} == {"Bearer token-of-ana|argv_leak=False"}
    assert set(seen["ben"].read_text().split("\n")) - {""} == {"Bearer token-of-ben|argv_leak=False"}
    assert _mcp_files() == files_before     # every per-run config file was removed

    bare, _ = asyncio.run(_run(tool_free_options("x", "haiku"), "Reply OK.", init_only=True))
    assert bare["tools"] == [] and bare["mcp_servers"] == []
