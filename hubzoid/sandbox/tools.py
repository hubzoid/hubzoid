"""The tools an agent with a sandbox uses, the same on every runtime.

* `exec_command` and `write_stdin` are the Agents SDK's own shell tools (JSON
  function tools), running commands, Python, search and git inside the
  sandbox, with long-running commands continuing in the background.
* `read_file`, `write_file`, `edit_file`, `glob` and `grep` are small JSON
  file tools over the same SDK session. They replace the SDK's `apply_patch`,
  which is an OpenAI-only custom tool, so Claude and Codex get identical tools.

Every call first attaches the sandbox: a missing sandbox is reported lost and
never recreated. Paths are inside /workspace (a relative path starts there),
checked with the real path inside the sandbox. Overwriting or editing a file
needs a current read of it. Output is scrubbed of credentials before the
model sees it. Errors come back as `{"error": {"code", "message"}}` so the
model can recover.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import io
import json
import shlex
import time
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

from sqlalchemy import text

from .. import redaction, tool_classes

NAMES = ("exec_command", "write_stdin", "read_file", "write_file", "edit_file", "glob", "grep")
#: Hub tools whose names a sandbox tool takes; an agent with a sandbox lists
#: them under these names instead.
HUB_ALIASES = {"read_hub_file": "read_file", "list_hub_files": "list_files"}
CLASSES = {"exec_command": "sandbox_local", "write_stdin": "sandbox_local", "write_file": "sandbox_local",
           "edit_file": "sandbox_local", "read_file": "read", "glob": "read", "grep": "read"}
MAX_WRITE = 1024 * 1024
MAX_OUTPUT = 50_000
#: Sandboxes opened during one agent run, closed when the run ends (see
#: `closing`), so no command it left running outlives its event loop.
_OPEN: ContextVar[list | None] = ContextVar("hubzoid_open_sandboxes", default=None)


def _err(code: str, message: str, **extra) -> str:
    return json.dumps({"error": {"code": code, "message": message, **extra}})


class Box:
    """One agent run's handle on its sandbox. Holds one SDK session, so a
    command left running can be reached again with `write_stdin`."""

    def __init__(self, hub_dir, sandbox_id: str):
        self.hub_dir, self.sandbox_id = Path(hub_dir), sandbox_id
        self._session = None
        self.row = None

    async def session(self):
        from . import service

        self.row = await asyncio.to_thread(service.attach, self.hub_dir, self.sandbox_id)
        if self._session is None:
            prov = service._provider_for(self.hub_dir, self.row)
            self._session = prov.session(self.row["provider_json"], self.sandbox_id)
            await self._session.start()
            opened = _OPEN.get()
            if opened is not None:
                opened.append(self)
        return self._session

    async def aclose(self) -> None:
        """End commands this run left running in the sandbox."""
        if self._session is not None:
            try:
                await self._session.pty_terminate_all()
            except Exception:  # noqa: BLE001 - the sandbox may already be gone
                pass
            self._session = None

    @property
    def lifecycle(self) -> dict:
        return (self.row or {}).get("profile_json", {}).get("lifecycle") or {}

    async def path(self, raw, *, for_write=False) -> PurePosixPath:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise _ToolError("invalid_path", "A path is text inside /workspace.")
        session = await self.session()
        candidate = raw if raw.startswith("/") else str(PurePosixPath("/workspace") / raw)
        try:
            resolved = await session._validate_path_access(candidate, for_write=for_write)
        except Exception:  # noqa: BLE001 - the SDK refuses with its own error types
            raise _ToolError("path_refused", f"{raw} is outside /workspace.") from None
        return PurePosixPath(str(resolved))

    def stamp(self, path) -> dict | None:
        from .service import _engine
        with _engine(self.hub_dir).connect() as c:
            row = c.execute(text("SELECT sha256, bytes FROM hz_sandbox_read_stamps WHERE sandbox_id=:s AND path=:p"),
                            {"s": self.sandbox_id, "p": str(path)}).mappings().first()
        return dict(row) if row else None

    def set_stamp(self, path, data: bytes) -> None:
        from .service import _engine
        sha = hashlib.sha256(data).hexdigest()
        with _engine(self.hub_dir).begin() as c:
            changed = c.execute(text("UPDATE hz_sandbox_read_stamps SET sha256=:h, bytes=:b, updated=:t "
                                     "WHERE sandbox_id=:s AND path=:p"),
                                {"s": self.sandbox_id, "p": str(path), "h": sha, "b": len(data),
                                 "t": time.time()}).rowcount
            if not changed:
                c.execute(text("INSERT INTO hz_sandbox_read_stamps (sandbox_id, path, sha256, bytes, updated) "
                               "VALUES (:s, :p, :h, :b, :t)"),
                          {"s": self.sandbox_id, "p": str(path), "h": sha, "b": len(data), "t": time.time()})


class _ToolError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def _tool(name: str, description: str, schema: dict, fn):
    from agents import FunctionTool

    async def invoke(_ctx, args_json: str) -> str:
        try:
            args = json.loads(args_json or "{}")
            if not isinstance(args, dict):
                raise _ToolError("invalid_request", "Arguments are a JSON object.")
            out = await fn(args)
            return out if isinstance(out, str) else json.dumps(out)
        except _ToolError as exc:
            return _err(exc.code, exc.message, **exc.extra)
        except Exception as exc:  # noqa: BLE001 - the model sees a short, scrubbed reason
            code = getattr(exc, "code", "sandbox_error")
            message = getattr(exc, "message", None) or redaction.scrub_error(exc, limit=500)
            return _err(code, message)

    tool = FunctionTool(name=name, description=description, params_json_schema=schema, on_invoke_tool=invoke,
                        strict_json_schema=False)
    return tool_classes.declare(tool, CLASSES[name])


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


MAX_READ = 8 * 1024 * 1024


async def _read_bytes(box: Box, path) -> bytes:
    """A regular file's bytes, refused before transfer when it is too large,
    and bounded in time."""
    session = await box.session()
    limit = min(MAX_READ, (box.row["profile_json"].get("transfer") or {}).get("max_file_bytes", MAX_READ))
    probe = await session.exec("stat", "-L", "-c", "%F %s", "--", str(path), shell=False, timeout=10)
    kind, _, size = probe.stdout.decode(errors="replace").strip().rpartition(" ")
    if probe.exit_code != 0:
        raise _ToolError("not_found", f"{path} does not exist.")
    if kind not in ("regular file", "regular empty file"):
        raise _ToolError("not_a_file", f"{path} is a {kind or 'special file'}, not a regular file.")
    if int(size or 0) > limit:
        raise _ToolError("file_too_large", f"{path} is {size} bytes; at most {limit} can be read. Use "
                         "exec_command with head, tail or grep for part of it.", size=int(size))
    res = await session.exec("head", "-c", str(limit + 1), "--", str(path), shell=False, timeout=30)
    if res.exit_code != 0:
        raise _ToolError("not_found", f"{path} could not be read.")
    if len(res.stdout) > limit:
        raise _ToolError("file_too_large", f"{path} grew past {limit} bytes while being read.")
    return res.stdout


async def _exists(box: Box, path) -> bool:
    session = await box.session()
    res = await session.exec("test", "-e", str(path), shell=False)
    return res.exit_code == 0


class closing:
    """`async with closing():` around one agent run: every sandbox session it
    opened is closed at the end, in the same event loop."""

    async def __aenter__(self):
        self._token = _OPEN.set([])
        return self

    async def __aexit__(self, *exc):
        boxes = _OPEN.get() or []
        _OPEN.reset(self._token)
        for box in boxes:
            await box.aclose()


def make(hub_dir, sandbox_id: str | None) -> dict:
    """{name: FunctionTool} for the sandbox. With no sandbox (validation and
    digests), the same tools refuse to run."""
    from agents.sandbox.capabilities.tools.shell_tool import (ExecCommandArgs, ExecCommandTool, WriteStdinArgs,
                                                              WriteStdinTool)

    box = Box(hub_dir, sandbox_id) if sandbox_id else None

    def need_box() -> Box:
        if box is None:
            raise _ToolError("sandbox_unavailable", "This agent has no sandbox.")
        return box

    async def exec_command(args):
        b = need_box()
        parsed = ExecCommandArgs.model_validate(args)
        session = await b.session()
        life = b.lifecycle
        cap_ms = int(life.get("command_timeout_seconds", 120)) * 1000
        parsed.yield_time_ms = min(parsed.yield_time_ms, cap_ms)
        parsed.cmd = (f"timeout -k 5 {int(life.get('max_job_seconds', 3600))} bash -c "
                      + shlex.quote(parsed.cmd))
        out = await ExecCommandTool(session=session).run(parsed)
        out = redaction.scrub_text(out)
        return out if len(out) <= MAX_OUTPUT else "[output truncated]\n" + out[-MAX_OUTPUT:]

    async def write_stdin(args):
        b = need_box()
        parsed = WriteStdinArgs.model_validate(args)
        session = await b.session()
        parsed.yield_time_ms = min(parsed.yield_time_ms, int(b.lifecycle.get("command_timeout_seconds", 120)) * 1000)
        out = redaction.scrub_text(await WriteStdinTool(session=session).run(parsed))
        return out if len(out) <= MAX_OUTPUT else "[output truncated]\n" + out[-MAX_OUTPUT:]

    async def read_file(args):
        b = need_box()
        path = await b.path(args.get("path"))
        data = await _read_bytes(b, path)
        if b"\x00" in data[:8192]:
            raise _ToolError("binary_file", f"{path} is a binary file ({len(data)} bytes).", size=len(data))
        await asyncio.to_thread(b.set_stamp, path, data)
        lines = data.decode("utf-8", errors="replace").splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(2000, int(args.get("limit") or 2000)))
        shown = lines[offset - 1: offset - 1 + limit]
        content = "\n".join(f"{offset + i:6}\t{line[:2000]}" for i, line in enumerate(shown))
        return {"path": str(path), "content": redaction.scrub_text(content), "start_line": offset,
                "end_line": offset + len(shown) - 1, "total_lines": len(lines),
                "truncated": offset - 1 + limit < len(lines), "sha256": hashlib.sha256(data).hexdigest()}

    async def _check_stamp(b: Box, path, current: bytes) -> None:
        stamp = await asyncio.to_thread(b.stamp, path)
        if stamp is None:
            raise _ToolError("read_required", f"Read {path} before changing it.")
        if stamp["sha256"] != hashlib.sha256(current).hexdigest() or stamp["bytes"] != len(current):
            raise _ToolError("stale_read", f"{path} changed since you read it. Read it again.")

    async def _write(b: Box, path, data: bytes) -> None:
        limit = min(MAX_WRITE, (b.row["profile_json"].get("transfer") or {}).get("max_file_bytes", MAX_WRITE))
        if len(data) > limit:
            raise _ToolError("file_too_large", f"At most {limit} bytes per write.")
        session = await b.session()
        await session.write(path, io.BytesIO(data))
        await asyncio.to_thread(b.set_stamp, path, data)

    async def write_file(args):
        b = need_box()
        content = args.get("content")
        if not isinstance(content, str):
            raise _ToolError("invalid_request", "content is text.")
        path = await b.path(args.get("path"), for_write=True)
        existed = await _exists(b, path)
        if existed:
            await _check_stamp(b, path, await _read_bytes(b, path))
        data = content.encode()
        await _write(b, path, data)
        return {"path": str(path), "existed": existed, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def edit_file(args):
        b = need_box()
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise _ToolError("invalid_request", "old_string (not empty) and new_string are text.")
        path = await b.path(args.get("path"), for_write=True)
        current = await _read_bytes(b, path)
        await _check_stamp(b, path, current)
        before = current.decode("utf-8", errors="strict") if b"\x00" not in current[:8192] else None
        if before is None:
            raise _ToolError("binary_file", f"{path} is a binary file.")
        count = before.count(old)
        if count == 0:
            raise _ToolError("no_match", "old_string does not occur in the file.")
        if count > 1 and not args.get("replace_all"):
            raise _ToolError("ambiguous_match", f"old_string occurs {count} times. Add context or set replace_all.",
                             count=count)
        after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
        if after == before:
            raise _ToolError("no_change", "The edit changes nothing.")
        await _write(b, path, after.encode())
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), str(path), str(path)))
        return {"path": str(path), "replacements": count if args.get("replace_all") else 1,
                "sha256": hashlib.sha256(after.encode()).hexdigest(),
                "diff": redaction.scrub_text(diff[:8192])}

    async def _rg(b: Box, argv: list[str], limit: int) -> list[str]:
        """At most `limit + 1` lines of output, so a huge result never reaches
        this process whole."""
        session = await b.session()
        command = " ".join(shlex.quote(a) for a in argv) + f" | head -n {limit + 1} | head -c 4194304"
        res = await session.exec("bash", "-c", "set -o pipefail; " + command, shell=False, timeout=60)
        if res.exit_code not in (0, 1, 141):          # 141: head closed the pipe early
            raise _ToolError("search_failed", redaction.scrub_text(res.stderr.decode(errors="replace"))[-500:])
        return res.stdout.decode("utf-8", errors="replace").splitlines()

    async def glob(args):
        b = need_box()
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise _ToolError("invalid_request", "pattern is a glob such as **/*.py.")
        root = await b.path(args.get("path") or ".")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        paths = await _rg(b, ["rg", "--files", "--sort", "path", "--glob", pattern, str(root)], limit)
        return {"paths": [redaction.scrub_text(p) for p in paths[:limit]], "truncated": len(paths) > limit}

    async def grep(args):
        b = need_box()
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise _ToolError("invalid_request", "pattern is a regular expression.")
        root = await b.path(args.get("path") or ".")
        limit = max(1, min(1000, int(args.get("limit") or 200)))
        argv = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-columns", "2000"]
        if args.get("ignore_case"):
            argv.append("--ignore-case")
        if args.get("glob"):
            argv += ["--glob", str(args["glob"])]
        lines = await _rg(b, argv + ["--regexp", pattern, "--", str(root)], limit)
        matches = []
        for line in lines[:limit]:
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                matches.append({"path": parts[0], "line": int(parts[1]),
                                "text": redaction.scrub_text(parts[2])[:2000]})
        return {"matches": matches, "truncated": len(lines) > limit}

    tools = [
        _tool("exec_command", ExecCommandTool.tool_description + " Runs inside your sandbox, offline, as an "
              "unprivileged user. Long commands keep running; poll them with write_stdin.",
              ExecCommandArgs.model_json_schema(), exec_command),
        _tool("write_stdin", WriteStdinTool.tool_description, WriteStdinArgs.model_json_schema(), write_stdin),
        _tool("read_file", "Read a text file in your sandbox, with line numbers. Read a file before you change it.",
              _obj({"path": {"type": "string", "description": "Inside /workspace; relative paths start there."},
                    "offset": {"type": "integer", "minimum": 1, "description": "First line, from 1."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]), read_file),
        _tool("write_file", "Create a file in your sandbox, or replace one you have read.",
              _obj({"path": {"type": "string"}, "content": {"type": "string", "description": "At most 1 MiB."}},
                   ["path", "content"]), write_file),
        _tool("edit_file", "Replace exact text in a file you have read. old_string must occur once unless "
              "replace_all is true.",
              _obj({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]), edit_file),
        _tool("glob", "List files in your sandbox matching a glob, such as **/*.py.",
              _obj({"pattern": {"type": "string"}, "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["pattern"]), glob),
        _tool("grep", "Search file contents in your sandbox with a regular expression.",
              _obj({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
                    "ignore_case": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
                   ["pattern"]), grep),
    ]
    return {t.name: t for t in tools}
