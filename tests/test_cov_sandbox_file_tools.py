"""The agent's sandbox tools (hubzoid.sandbox.tools) over a fake sandbox
session (no Docker): paths stay inside /workspace; a file must be read before
it is replaced or edited, and a file changed since is refused; binary, too
large and missing files are refused with a code the model can act on; edits
must match once (or replace_all) and must change something; glob and grep cap
their output; commands are wrapped in the job time limit and long output is
cut; errors come back as {"error": {...}} with the raw exception scrubbed;
every session a run opened is closed when it ends."""
from __future__ import annotations

import asyncio
import json
import posixpath
from types import SimpleNamespace

import pytest

from hubzoid import db, migrations
from hubzoid.sandbox import service
from hubzoid.sandbox import tools as st


class Session:
    """A sandbox session over an in-memory /workspace."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs = {"/workspace"}
        self.rg_out, self.rg_code, self.commands, self.terminated = b"", 0, [], 0

    async def start(self):
        return None

    async def pty_terminate_all(self):
        self.terminated += 1
        raise RuntimeError("container already gone")              # never stops the close

    async def _validate_path_access(self, candidate, for_write=False):
        norm = posixpath.normpath(candidate)
        if norm != "/workspace" and not norm.startswith("/workspace/"):
            raise PermissionError("outside the workspace")
        return norm

    async def write(self, path, stream):
        self.files[str(path)] = stream.read()

    async def exec(self, *argv, shell=False, timeout=None):
        self.commands.append(argv)
        ok = SimpleNamespace(exit_code=0, stdout=b"", stderr=b"")
        if argv[0] == "stat":
            path = argv[-1]
            if path in self.files:
                size = len(self.files[path])
                kind = "regular file" if size else "regular empty file"
                return SimpleNamespace(exit_code=0, stdout=f"{kind} {size}".encode(), stderr=b"")
            if path in self.dirs:
                return SimpleNamespace(exit_code=0, stdout=b"directory 4096", stderr=b"")
            return SimpleNamespace(exit_code=1, stdout=b"", stderr=b"No such file")
        if argv[0] == "head":
            return SimpleNamespace(exit_code=0, stdout=self.files[argv[-1]][:int(argv[2])], stderr=b"")
        if argv[0] == "test":
            return SimpleNamespace(exit_code=0 if argv[-1] in self.files else 1, stdout=b"", stderr=b"")
        if argv[0] == "bash":
            return SimpleNamespace(exit_code=self.rg_code, stdout=self.rg_out, stderr=b"rg: regex parse error")
        return ok


@pytest.fixture
def box(tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    hub = tmp_path / "boxhub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: Box\n---\nA hub.\n")
    migrations.upgrade(db.operational_engine(hub), "operational")
    session = Session()
    row = {"id": "sb-1", "provider_json": {}, "profile_json": {
        "lifecycle": {"command_timeout_seconds": 30, "max_job_seconds": 600},
        "transfer": {"max_file_bytes": 64}}}
    monkeypatch.setattr(service, "attach", lambda hub_dir, sandbox_id: row)
    monkeypatch.setattr(service, "_provider_for", lambda hub_dir, r: SimpleNamespace(
        session=lambda provider_json, sandbox_id: session))
    return SimpleNamespace(tools=st.make(hub, "sb-1"), session=session, hub=hub)


def call(tools, name, args):
    out = asyncio.run(tools[name].on_invoke_tool(None, json.dumps(args)))
    return json.loads(out) if out.startswith("{") else out


def code(out):
    return out["error"]["code"]


def test_paths_stay_inside_the_workspace(box):
    assert code(call(box.tools, "read_file", {"path": "../etc/passwd"})) == "path_refused"
    assert code(call(box.tools, "read_file", {"path": "/etc/passwd"})) == "path_refused"
    assert code(call(box.tools, "read_file", {"path": "a\x00b"})) == "invalid_path"
    assert code(call(box.tools, "read_file", {"path": ""})) == "invalid_path"
    out = asyncio.run(box.tools["read_file"].on_invoke_tool(None, "[1]"))
    assert code(json.loads(out)) == "invalid_request"


def test_reading_files(box):
    box.session.files["/workspace/notes.txt"] = b"one\ntwo\nthree\n"
    out = call(box.tools, "read_file", {"path": "notes.txt", "offset": 2, "limit": 1})
    assert out["content"] == "     2\ttwo" and (out["start_line"], out["end_line"], out["total_lines"]) == (2, 2, 3)
    assert out["truncated"] is True and out["path"] == "/workspace/notes.txt"
    box.session.files["/workspace/logo.png"] = b"\x89PNG\x00\x00"
    assert code(call(box.tools, "read_file", {"path": "logo.png"})) == "binary_file"
    box.session.files["/workspace/big.csv"] = b"x" * 65
    big = call(box.tools, "read_file", {"path": "big.csv"})
    assert code(big) == "file_too_large" and big["error"]["size"] == 65
    assert code(call(box.tools, "read_file", {"path": "missing.txt"})) == "not_found"
    box.session.dirs.add("/workspace/src")
    assert "is a directory" in call(box.tools, "read_file", {"path": "src"})["error"]["message"]


def test_a_file_is_read_before_it_is_replaced(box):
    tools, files = box.tools, box.session.files
    made = call(tools, "write_file", {"path": "new.txt", "content": "hello"})
    assert made["existed"] is False and made["bytes"] == 5 and files["/workspace/new.txt"] == b"hello"
    files["/workspace/report.md"] = b"draft"
    assert code(call(tools, "write_file", {"path": "report.md", "content": "final"})) == "read_required"
    call(tools, "read_file", {"path": "report.md"})
    files["/workspace/report.md"] = b"changed by a command"
    assert code(call(tools, "write_file", {"path": "report.md", "content": "final"})) == "stale_read"
    call(tools, "read_file", {"path": "report.md"})
    assert call(tools, "write_file", {"path": "report.md", "content": "final"})["existed"] is True
    assert code(call(tools, "write_file", {"path": "x.txt", "content": "y" * 65})) == "file_too_large"
    assert code(call(tools, "write_file", {"path": "x.txt", "content": 5})) == "invalid_request"


def test_editing_files(box):
    tools, files = box.tools, box.session.files
    files["/workspace/app.py"] = b"a = 1\nb = 1\n"
    assert code(call(tools, "edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})) == "read_required"
    call(tools, "read_file", {"path": "app.py"})
    amb = call(tools, "edit_file", {"path": "app.py", "old_string": "1", "new_string": "2"})
    assert code(amb) == "ambiguous_match" and amb["error"]["count"] == 2
    assert code(call(tools, "edit_file", {"path": "app.py", "old_string": "zzz", "new_string": "2"})) == "no_match"
    assert code(call(tools, "edit_file", {"path": "app.py", "old_string": "a", "new_string": "a"})) == "no_change"
    assert code(call(tools, "edit_file", {"path": "app.py", "old_string": "", "new_string": "2"})) == "invalid_request"
    out = call(tools, "edit_file", {"path": "app.py", "old_string": "1", "new_string": "2", "replace_all": True})
    assert out["replacements"] == 2 and files["/workspace/app.py"] == b"a = 2\nb = 2\n"
    assert "-a = 1\n-b = 1\n+a = 2\n+b = 2" in out["diff"]
    # A binary file is never read through read_file, but a stamp may exist
    # from a write: editing it is still refused.
    files["/workspace/blob.bin"] = b"\x00\x01"
    st.Box(box.hub, "sb-1").set_stamp("/workspace/blob.bin", b"\x00\x01")
    assert code(call(tools, "edit_file", {"path": "blob.bin", "old_string": "a", "new_string": "b"})) == "binary_file"


def test_glob_and_grep_cap_their_output(box):
    tools, session = box.tools, box.session
    session.rg_out = b"/workspace/a.py\n/workspace/b.py\n/workspace/c.py\n"
    out = call(tools, "glob", {"pattern": "**/*.py", "limit": 2})
    assert out == {"paths": ["/workspace/a.py", "/workspace/b.py"], "truncated": True}
    assert "head -n 3" in session.commands[-1][-1]
    session.rg_out = b"/workspace/a.py:3:def main():\nnot a match line\n"
    out = call(tools, "grep", {"pattern": "def", "ignore_case": True, "glob": "*.py"})
    assert out == {"matches": [{"path": "/workspace/a.py", "line": 3, "text": "def main():"}], "truncated": False}
    assert "--ignore-case" in session.commands[-1][-1] and "--glob" in session.commands[-1][-1]
    session.rg_code = 2
    assert code(call(tools, "grep", {"pattern": "("})) == "search_failed"
    assert code(call(tools, "glob", {"pattern": ""})) == "invalid_request"
    assert code(call(tools, "grep", {"pattern": None})) == "invalid_request"


def test_commands_run_within_the_job_limit_and_long_output_is_cut(box, monkeypatch):
    from agents.sandbox.capabilities.tools import shell_tool

    seen = []

    async def run(self, parsed):
        seen.append(parsed)
        return "x" * (st.MAX_OUTPUT + 10)

    monkeypatch.setattr(shell_tool.ExecCommandTool, "run", run)
    monkeypatch.setattr(shell_tool.WriteStdinTool, "run", run)
    out = call(box.tools, "exec_command", {"cmd": "make test", "yield_time_ms": 999_999})
    assert out.startswith("[output truncated]\n") and len(out) == len("[output truncated]\n") + st.MAX_OUTPUT
    assert seen[0].cmd == "timeout -k 5 600 bash -c 'make test'" and seen[0].yield_time_ms == 30_000
    call(box.tools, "write_stdin", {"session_id": 1, "chars": "y\n", "yield_time_ms": 999_999})
    assert seen[1].yield_time_ms == 30_000


def test_no_sandbox_and_unexpected_errors(tmp_path, box, monkeypatch):
    none = st.make(tmp_path, None)
    assert code(call(none, "read_file", {"path": "a"})) == "sandbox_unavailable"

    def lost(hub_dir, sandbox_id):
        raise RuntimeError("container sb-1 vanished; token=sk-live-abcdefghijklmnopqrstuvwxyz0123")

    monkeypatch.setattr(service, "attach", lost)
    fresh = st.make(box.hub, "sb-1")
    out = call(fresh, "read_file", {"path": "a"})
    assert code(out) == "sandbox_error" and "sk-live-abcdefghijklmnopqrstuvwxyz0123" not in out["error"]["message"]


def test_a_run_closes_every_session_it_opened(box):
    box.session.files["/workspace/a.txt"] = b"a"

    async def run():
        async with st.closing():
            await box.tools["read_file"].on_invoke_tool(None, json.dumps({"path": "a.txt"}))

    asyncio.run(run())
    assert box.session.terminated == 1
