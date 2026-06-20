"""The restricted/ folder must be unreadable by the file-reading tools.

This is the credential boundary: secrets live in `restricted/.env`, and the
model's file tools (read_file, list_files, grep_data) must refuse to read
anything under restricted/, so the model cannot exfiltrate a credential by
reading the file even though it cannot call the gated tools.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from hubzoid import _fs
from hubzoid.tools import files as files_tool
from hubzoid.tools import grep_data as gd


class _Ctx:
    def __init__(self, hub_dir: Path):
        self.hub_dir = hub_dir
        self.output_dir = hub_dir / "output" / "test-session"


def _invoke(tool, **kwargs) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def _file_tools(hub: Path) -> dict:
    return {t.name: t for t in files_tool.make(_Ctx(hub))}


def _seed(hub: Path):
    (hub / "restricted").mkdir()
    (hub / "restricted" / ".env").write_text("ORNATE_PASSWORD=supersecret\n")
    (hub / "restricted" / "ornate.py").write_text("# tool code with no secret\n")
    (hub / "notes.txt").write_text("just a public note\n")
    rd = hub / "raw_data"
    rd.mkdir()
    (rd / "data.txt").write_text("ORNATE_PASSWORD lookalike but harmless\n")


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------
def test_is_under_restricted(tmp_path):
    _seed(tmp_path)
    assert _fs.is_under_restricted(tmp_path, tmp_path / "restricted" / ".env")
    assert _fs.is_under_restricted(tmp_path, tmp_path / "restricted")
    assert not _fs.is_under_restricted(tmp_path, tmp_path / "notes.txt")


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
def test_read_file_refuses_restricted_env(tmp_path):
    _seed(tmp_path)
    out = _invoke(_file_tools(tmp_path)["read_file"], path="restricted/.env")
    assert "refused" in out.lower()
    assert "supersecret" not in out


def test_read_file_still_reads_public_file(tmp_path):
    _seed(tmp_path)
    out = _invoke(_file_tools(tmp_path)["read_file"], path="notes.txt")
    assert "public note" in out


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------
def test_list_files_hides_restricted(tmp_path):
    _seed(tmp_path)
    out = _invoke(_file_tools(tmp_path)["list_files"], glob="**/*")
    assert "notes.txt" in out
    assert "restricted" not in out
    assert ".env" not in out


# ---------------------------------------------------------------------------
# grep_data
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _force_python_backend(monkeypatch):
    monkeypatch.setattr(gd.shutil, "which", lambda _: None)


def test_grep_refuses_restricted_folder(tmp_path):
    _seed(tmp_path)
    tool = gd.make(_Ctx(tmp_path))[0]
    out = _invoke(tool, pattern="ORNATE_PASSWORD", path="restricted")
    assert "refused" in out.lower()
    assert "supersecret" not in out


def test_grep_refuses_restricted_env_directly(tmp_path):
    _seed(tmp_path)
    tool = gd.make(_Ctx(tmp_path))[0]
    out = _invoke(tool, pattern="ORNATE_PASSWORD", path="restricted/.env")
    assert "refused" in out.lower()
    assert "supersecret" not in out


def test_grep_still_searches_raw_data(tmp_path):
    _seed(tmp_path)
    tool = gd.make(_Ctx(tmp_path))[0]
    out = _invoke(tool, pattern="lookalike", path="raw_data")
    assert "data.txt" in out
