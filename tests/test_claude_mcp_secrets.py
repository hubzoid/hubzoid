"""Secret-bearing MCP configuration never reaches the `claude` command line.

External MCP servers (the hub's `.mcp.json`, each person's Open WebUI
connector) go to a private 0600 file per run, passed as a second
`--mcp-config`; the argument keeps only the in-process Hubzoid server. The
file holds only that caller's connectors and is removed on success, failure
and cancellation. Synthetic credentials only; no model calls. The live check is
tests/e2e/test_claude_mcp_isolation_e2e.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys

import claude_agent_sdk
import pytest
from claude_agent_sdk import ResultMessage

from hubzoid import factory_claude, owui_mcp
from hubzoid.access import Identity, identity_scope, store_for
from hubzoid.factory_claude import PrivateMcpConfig
from tests.test_claude_mcp_isolation import _cli

HUB_SECRET, HEADER_SECRET = "hub-env-SECRET-1111", "hub-header-SECRET-2222"
TOKENS = {"ana": "person-token-ANA-3333", "ben": "person-token-BEN-4444"}
ALL_SECRETS = [HUB_SECRET, HEADER_SECRET, *TOKENS.values()]


@pytest.fixture
def rt(tmp_path, monkeypatch):
    d = tmp_path / "support"
    (d / "connectors").mkdir(parents=True)
    (d / "AGENTS.md").write_text("---\nname: support\ndescription: d\n---\nHelp.\n")
    (d / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "hub-echo": {"command": sys.executable, "args": ["-m", "synthetic"], "env": {"API_TOKEN": HUB_SECRET}},
        "hub-api": {"transport": "streamable-http", "url": "http://127.0.0.1:9/mcp",
                    "headers": {"Authorization": f"Bearer {HEADER_SECRET}"}}}}))
    for k in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_BROWSER", "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    store_for(d).set_authoritative(True, hub="support")

    def specs(hub_dir, ident):
        who = (ident.user or "").split("@")[0]
        if who not in TOKENS:
            return {}, []
        return ({f"notes-{who}": {"type": "http", "url": f"http://127.0.0.1:9/{who}",
                                  "headers": {"Authorization": f"Bearer {TOKENS[who]}"}}},
                [f"mcp__notes-{who}__*"])

    monkeypatch.setattr(owui_mcp, "per_user_specs", specs)
    return factory_claude.build_claude_runtime(d)


def _turn_options(rt, who):
    with identity_scope(Identity.make(f"{who}@example.org", surface="owui")):
        return rt._options_for_turn()


# --- the file ------------------------------------------------------------------------
def test_secrets_leave_argv_for_a_private_file(rt):
    before = " ".join(_cli(_turn_options(rt, "ana")))
    assert HUB_SECRET in before and TOKENS["ana"] in before        # the exposure being fixed

    with PrivateMcpConfig(_turn_options(rt, "ana")) as cfg:
        argv = _cli(cfg.options)
        assert not [s for s in ALL_SECRETS if s in " ".join(argv)]
        assert "--strict-mcp-config" in argv
        assert argv.count("--mcp-config") == 2 and cfg.path in argv
        in_arg = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
        assert set(in_arg) == {"hubzoid"} and in_arg["hubzoid"]["type"] == "sdk"
        assert cfg.options.mcp_servers["hubzoid"]["instance"] is not None   # in-process tools kept
        assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600
        on_disk = json.load(open(cfg.path))["mcpServers"]
        assert set(on_disk) == {"hub-echo", "hub-api", "notes-ana"}
        assert on_disk["hub-echo"]["env"]["API_TOKEN"] == HUB_SECRET
        assert on_disk["notes-ana"]["headers"]["Authorization"] == f"Bearer {TOKENS['ana']}"
        path = cfg.path
    assert not os.path.exists(path)


def test_each_run_gets_its_own_file_with_only_its_callers_connector(rt):
    ana, ben = PrivateMcpConfig(_turn_options(rt, "ana")), PrivateMcpConfig(_turn_options(rt, "ben"))
    try:
        assert ana.path != ben.path
        assert TOKENS["ben"] not in open(ana.path).read()
        assert TOKENS["ana"] not in open(ben.path).read()
        again = PrivateMcpConfig(_turn_options(rt, "ana"))
        assert again.path not in (ana.path, ben.path)
        again.close()
    finally:
        ana.close()
        ben.close()
    assert not os.path.exists(ana.path or "") and ana.path is None


def test_no_external_servers_means_no_file(rt, monkeypatch):
    opts = factory_claude.tool_free_options("x")
    cfg = PrivateMcpConfig(opts)
    assert cfg.path is None and cfg.options is opts


def test_a_failed_write_leaves_no_file(rt, monkeypatch, tmp_path):
    import tempfile

    made = []
    real = tempfile.mkstemp

    def mkstemp(**kw):
        fd, path = real(dir=tmp_path, **kw)
        made.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        PrivateMcpConfig(_turn_options(rt, "ana"))
    assert made and not os.path.exists(made[0])


# --- a real chat turn (ClaudeRuntime.stream) cleans up in every outcome ---------------
def _stream(rt, who, fake_query, monkeypatch):
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query, raising=False)

    async def go():
        with identity_scope(Identity.make(f"{who}@example.org", surface="owui")):
            return "".join([part async for part in rt.stream("hello")])

    return asyncio.run(go())


def _ok():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s", result="done")


def test_turn_success_uses_the_file_then_removes_it(rt, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    seen = {}

    async def fake_query(*, prompt, options):
        seen["path"] = options.extra_args["mcp-config"]
        seen["mode"] = stat.S_IMODE(os.stat(seen["path"]).st_mode)
        seen["file"] = open(seen["path"]).read()
        seen["argv"] = " ".join(_cli(options))
        yield _ok()

    text = _stream(rt, "ana", fake_query, monkeypatch)
    assert "done" in text
    assert seen["mode"] == 0o600 and TOKENS["ana"] in seen["file"] and TOKENS["ben"] not in seen["file"]
    assert not [s for s in ALL_SECRETS if s in seen["argv"]]
    assert not os.path.exists(seen["path"])
    assert not [s for s in ALL_SECRETS if s in caplog.text]


def test_turn_failure_removes_the_file(rt, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    seen = {}

    async def fake_query(*, prompt, options):
        seen["path"] = options.extra_args["mcp-config"]
        raise RuntimeError("CLI exited with code 1")
        yield  # pragma: no cover

    text = _stream(rt, "ben", fake_query, monkeypatch)
    assert "[agent error: RuntimeError: CLI exited with code 1]" in text
    assert not os.path.exists(seen["path"])
    assert not [s for s in ALL_SECRETS if s in caplog.text]


def test_turn_cancellation_removes_the_file(rt, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options):
        seen["path"] = options.extra_args["mcp-config"]
        seen["entered"].set()
        await asyncio.Event().wait()     # a turn that never finishes
        yield _ok()                      # pragma: no cover

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query, raising=False)

    async def go():
        seen["entered"] = asyncio.Event()

        async def consume():
            with identity_scope(Identity.make("ana@example.org", surface="owui")):
                async for _ in rt.stream("hello"):
                    pass

        task = asyncio.create_task(consume())
        await seen["entered"].wait()
        assert os.path.exists(seen["path"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert not os.path.exists(seen["path"])
