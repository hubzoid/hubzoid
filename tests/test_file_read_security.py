"""Synthetic secrets only: public file tools must not expose operator state."""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from hubzoid.tools import files, grep_data

CANARY = "PRIVATE_CREDENTIAL_CANARY_8291"
RG = shutil.which("rg")


def invoke(tool, **args):
    payload = json.dumps(args)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="security-test", tool_arguments=payload)
    return asyncio.run(tool.on_invoke_tool(ctx, payload))


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()
    (root / "AGENTS.md").write_text("A test agent.")
    (root / "raw_data").mkdir()
    (root / "raw_data" / "public.txt").write_text("public needle\n")
    return root


def registry(hub):
    ctx = SimpleNamespace(hub_dir=hub, output_dir=hub / "output" / "test")
    return {t.name: t for t in [*files.make(ctx), *grep_data.make(ctx)]}


PRIVATE_PATHS = [
    ".env", ".env.production", ".ENV.local", "config/service.env", "config/service.env.bak",
    ".openwebui-data/webui.db", "backup/webui.DB", "backup/users.sqlite3",
    "backup/users.sqlite-wal", "backup/users.db-shm", "backup/users.db-journal",
    ".hubzoid/deployment.json", ".git/config", "ReStRiCtEd/notes.txt",
    "raw_data/restricted/.env", "raw_data/restricted/tool.py",
]


@pytest.mark.parametrize("name", PRIVATE_PATHS)
def test_read_and_list_hide_private_files(hub, name):
    p = hub / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CANARY)
    tools = registry(hub)
    for path in (name, str(p)):
        result = invoke(tools["read_file"], path=path, offset=1, limit=500)
        assert "refused" in result.lower()
        assert CANARY not in result
    assert name not in invoke(tools["list_files"], glob="**/*")


@pytest.mark.parametrize("backend", ["python", "rg"])
def test_recursive_search_never_reads_private_files(hub, monkeypatch, backend):
    if backend == "rg" and RG is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: RG if backend == "rg" else None)
    for name in PRIVATE_PATHS:
        p = hub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"needle {CANARY}\n")
    tool = registry(hub)["grep_data"]
    result = invoke(tool, pattern="needle", path=".", context=2)
    assert "public needle" in result
    assert CANARY not in result
    for name in PRIVATE_PATHS:
        assert "refused" in invoke(tool, pattern="needle", path=name).lower()
    # No canary in overflow files either.
    assert all(CANARY not in p.read_text() for p in (hub / "output").rglob("*.txt"))


@pytest.mark.parametrize("target", [".env", "state.db", "restricted/notes.txt", "../outside.txt"])
def test_symlinks_cannot_launder_private_or_external_files(hub, monkeypatch, target):
    p = hub / target
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CANARY)
    link = hub / "raw_data" / "innocent.txt"
    link.symlink_to(p)
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: None)
    tools = registry(hub)
    assert "refused" in invoke(tools["read_file"], path="raw_data/innocent.txt")
    assert "innocent.txt" not in invoke(tools["list_files"], glob="**/*")
    assert CANARY not in invoke(tools["grep_data"], pattern=CANARY, path=".")


def test_sqlite_cannot_be_read_after_renaming(hub):
    p = hub / "raw_data" / "export.txt"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE tokens(secret TEXT)")
        db.execute("INSERT INTO tokens VALUES (?)", (CANARY,))
    tools = registry(hub)
    assert "refused" in invoke(tools["read_file"], path="raw_data/export.txt")
    assert "export.txt" not in invoke(tools["list_files"], glob="**/*")


def test_mcp_registry_has_the_same_file_boundary(hub):
    from hubzoid.mcp_server import build_registry
    (hub / ".env").write_text(f"FAKE_SECRET={CANARY}\n")
    tools, _ = build_registry(hub)
    assert "refused" in invoke(tools["read_file"], path=".env")
    assert CANARY not in invoke(tools["grep_data"], pattern=CANARY, path=".")
    assert "public.txt" in invoke(tools["list_files"], glob="raw_data/*")


@pytest.mark.parametrize("bucket", ["knowledge", "skills", "agents"])
def test_markdown_alias_cannot_publish_a_secret(hub, bucket):
    from hubzoid.loaders import knowledge, skills, agents
    (hub / ".env").write_text(CANARY)
    folder = hub / bucket
    folder.mkdir()
    (folder / "public.md").symlink_to(hub / ".env")
    load = {"knowledge": knowledge.load_all, "skills": skills.load_hub, "agents": agents.load_subagents}[bucket]
    assert load(hub) == []


def test_template_cannot_bypass_file_tools(hub):
    from hubzoid.tools import render
    p = hub / ".env"
    p.write_text(CANARY)
    template = "{{ cycler.__init__.__globals__.__builtins__.open(" + repr(str(p)) + ").read() }}"
    result = invoke(render.make(None)[0], template=template)
    assert CANARY not in result
    assert "render_jinja:" in result


def test_current_chat_files_still_work_but_secrets_do_not(hub):
    from hubzoid import _request_ctx, memory
    with _request_ctx.chat_scope("security-current-chat"):
        uploads = memory.chat_upload_dir(hub, "security-current-chat")
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / "notes.txt").write_text("my uploaded notes")
        (uploads / ".env").write_text(CANARY)
        other = memory.chat_upload_dir(hub, "other-chat")
        other.mkdir(parents=True, exist_ok=True)
        (other / "notes.txt").write_text(CANARY)
        tools = registry(hub)
        own_path = str((uploads / "notes.txt").relative_to(hub))
        assert invoke(tools["read_file"], path=own_path) == "my uploaded notes"
        assert "my uploaded notes" in invoke(tools["read_upload"], filename="notes.txt")
        for tool in ("read_upload", "read_upload_full"):
            assert "refused" in invoke(tools[tool], filename=".env")
        assert "refused" in invoke(tools["read_file"], path=str(other / "notes.txt"))


@pytest.mark.parametrize("backend", ["python", "rg"])
def test_search_preserves_context_and_safe_links(hub, monkeypatch, backend):
    if backend == "rg" and RG is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: RG if backend == "rg" else None)
    p = hub / "raw_data" / "a:note.txt"
    p.write_text("before\nneedle\nafter\n")
    (hub / "raw_data" / "alias.txt").symlink_to(p)
    result = invoke(registry(hub)["grep_data"], pattern="needle", context=1)
    assert "a:note.txt:1:before" in result
    assert "a:note.txt:3:after" in result
    linked = invoke(registry(hub)["grep_data"], pattern="needle", path="raw_data/alias.txt")
    assert "a:note.txt:2:needle" in linked


def test_relative_hub_root_still_checks_database_signature(hub, monkeypatch):
    p = hub / "raw_data" / "export.txt"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE tokens(secret TEXT)")
    monkeypatch.chdir(hub.parent)
    tools = registry(Path(hub.name))
    assert "refused" in invoke(tools["read_file"], path="raw_data/export.txt")
    assert "public needle" in invoke(tools["read_file"], path="raw_data/public.txt")


@pytest.mark.parametrize("backend", ["python", "rg"])
def test_search_overflow_does_not_spill_secrets(hub, monkeypatch, backend):
    if backend == "rg" and RG is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: RG if backend == "rg" else None)
    monkeypatch.setattr(grep_data, "RESULT_CAP", 50)
    (hub / "raw_data" / ".env").write_text(f"needle {CANARY}")
    (hub / "raw_data" / "public.txt").write_text("needle " + "public " * 100)
    result = invoke(registry(hub)["grep_data"], pattern="needle", path=".")
    assert "Result truncated" in result
    assert CANARY not in result
    spills = list((hub / "output").rglob("grep-overflow-*.txt"))
    assert spills
    assert all(CANARY not in p.read_text() for p in spills)


def test_ripgrep_cap_still_reports_hidden_matches(hub, monkeypatch):
    if RG is None:
        pytest.skip("ripgrep is not installed")
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: RG)
    (hub / "raw_data" / "public.txt").write_text("needle\n" * 100)
    result = invoke(registry(hub)["grep_data"], pattern="needle")
    assert "more matches than shown" in result
