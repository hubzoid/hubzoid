"""Every tool a runtime can receive has a schema where only truly required
arguments are required, on every runtime (OpenAI, claude-local, codex-local
``dynamicTools`` and MCP get the same ``params_json_schema``).

A strict schema marks optional arguments required, and Claude, Codex and MCP
clients then refuse a call that leaves them out (the recurring ``read_upload``
failure). This walks the tools each runtime actually receives, not just the
users of ``hubzoid.tool``, so a new strict tool cannot bring the bug back.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


def _ctx(hub: Path):
    from hubzoid import factory
    from hubzoid import memory as memlib
    from hubzoid import settings as settingslib
    from hubzoid.loaders import knowledge as knowledge_loader

    sid = memlib.make_session_id()
    return factory.HubContext(
        hub_dir=hub, output_dir=memlib.session_output_dir(hub, sid), session_id=sid,
        settings=settingslib.load(hub), skills=factory._load_skills_and_promoted_agents(hub),
        knowledge=knowledge_loader.load_all(hub), delegates=[])


def _every_tool(hub: Path) -> dict:
    """Built-ins (chat-scoped ones included), hub-local tools, restricted
    wrappers as MCP serves them, and the scheduler's delivery tools."""
    from hubzoid import mcp_server
    from hubzoid.loaders import tools_local
    from hubzoid.tools import make_all
    from hubzoid.tools import schedule_tools

    out = dict(make_all(_ctx(hub)))
    out.update(tools_local.load_all(hub))
    registry, _ = mcp_server.build_registry(hub)
    out.update({f"mcp:{k}": v for k, v in registry.items()})
    task = SimpleNamespace(publish_artifacts=True, send_email=True, run_identity={}, name="t", run_id="r")
    for t in schedule_tools._delivery_tools(hub, task, lambda **_: None, lambda p: Path(p)):
        out[f"schedule:{t.name}"] = t
    return out


@pytest.fixture(scope="module")
def tools():
    found = _every_tool(MINIMAL)
    assert {"read_upload", "read_upload_full", "schedule:send_email"} <= set(found)
    return found


def test_no_tool_marks_an_optional_argument_required(tools):
    from hubzoid._tool import optional_marked_required

    bad = {name: optional_marked_required(t) for name, t in tools.items() if optional_marked_required(t)}
    assert bad == {}


def test_required_arguments_have_no_default(tools):
    bad = {}
    for name, t in tools.items():
        schema = t.params_json_schema or {}
        props = schema.get("properties") or {}
        wrong = [a for a in schema.get("required") or [] if "default" in (props.get(a) or {})]
        if wrong:
            bad[name] = wrong
    assert bad == {}


def test_strict_flag_matches_the_schema(tools):
    """The OpenAI runtime sends ``strict`` from this flag: a relaxed schema
    marked strict would be rejected by the API."""
    for name, t in tools.items():
        if t.strict_json_schema:
            schema = t.params_json_schema or {}
            assert set(schema.get("required") or []) == set(schema.get("properties") or {}), name


def test_read_upload_requires_only_the_filename(tools):
    for name in ("read_upload", "read_upload_full"):
        assert tools[name].params_json_schema["required"] == ["filename"], name
    send = tools["schedule:send_email"].params_json_schema
    assert sorted(send["required"]) == ["body", "subject"]


def test_claude_and_mcp_receive_the_same_schema(tools):
    from hubzoid import factory_claude

    ft = tools["read_upload"]
    assert factory_claude._to_claude_tool(ft).input_schema == ft.params_json_schema


def test_a_call_that_leaves_optional_arguments_out_runs(tmp_path, monkeypatch):
    """The real failure: the first read of a CSV with only ``filename``."""
    import shutil

    from hubzoid import memory as memlib
    from hubzoid._request_ctx import set_chat_id
    from hubzoid.tools import files

    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub)
    monkeypatch.setenv("HUBZOID_DATA_DIR", str(tmp_path / "data"))
    set_chat_id("schema-chat")
    folder = memlib.chat_upload_dir(hub, "schema-chat")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "orders.csv").write_text("id,total\n1,10\n2,32\n")
    read_upload = {t.name: t for t in files.make(_ctx(hub))}["read_upload"]
    from agents.tool_context import ToolContext

    tc = ToolContext(context=None, tool_name="read_upload", tool_call_id="c1",
                     tool_arguments=json.dumps({"filename": "orders.csv"}))
    out = asyncio.run(read_upload.on_invoke_tool(tc, json.dumps({"filename": "orders.csv"})))
    assert "2,32" in str(out)


def test_hub_tool_loader_names_strict_tools(tmp_path):
    from hubzoid.loaders import tools_local

    (tmp_path / "AGENTS.md").write_text("hi")
    (tmp_path / "tools_local").mkdir()
    (tmp_path / "tools_local" / "old.py").write_text(
        "from agents import function_tool\n\n"
        "@function_tool\ndef lookup(order_id: str, limit: int | None = None) -> str:\n"
        "    '''Look up.'''\n    return order_id\n")
    (tmp_path / "tools_local" / "new.py").write_text(
        "from hubzoid import tool\n\n"
        "@tool\ndef find(order_id: str, limit: int | None = None) -> str:\n"
        "    '''Find.'''\n    return order_id\n")
    loaded = tools_local.load_all(tmp_path)
    assert set(loaded) == {"lookup", "find"}
    assert tools_local.STRICT_TOOLS == {"lookup": "tools_local/old.py"}
    assert loaded["find"].params_json_schema["required"] == ["order_id"]
