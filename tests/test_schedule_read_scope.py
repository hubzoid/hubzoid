"""Scheduled working files stay useful without exposing other private state."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from hubzoid import _request_ctx, schedule_runner, scheduling
from hubzoid.access import store_for
from hubzoid.tools import files, grep_data, schedule_tools
from hubzoid.workflows.identity import RunIdentity, markdown_scratch

ALICE, BOB = "alice@example.org", "bob@example.org"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path}/ops.db")
    for key in ("DATABASE_URL", "HUBZOID_DEPLOYMENT", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "hub"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: hub\n---\nSynthetic test")
    (root / "raw_data").mkdir()
    gs = store_for(root)
    for who in (ALICE, BOB):
        gs.upsert_identity(email=who, owui_id=who)
        gs.grant(who, "hub", "use_hub", actor="test")
    return root


def ident(who):
    return RunIdentity(who, who, "run_as", who)


def task(name="daily", **kwargs):
    return scheduling.ScheduledTask(name=name, schedule="0 3 * * *",
                                   cron=scheduling.parse_cron("0 3 * * *"),
                                   body="Resume from your state and notes.", **kwargs)


def builtins(hub):
    ctx = SimpleNamespace(hub_dir=hub, output_dir=hub / "output")
    return {t.name: t for t in files.make(ctx) + grep_data.make(ctx)}


async def invoke(tool, **args):
    raw = json.dumps(args)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="test",
                      tool_arguments=raw)
    return await tool.on_invoke_tool(ctx, raw)


async def run(hub, declaration, who, action):
    def factory(root, selected, emit):
        registry = {**builtins(root), **{t.name: t for t in schedule_tools.make(root, selected, emit)}}

        class Runtime:
            async def aopen(self):
                # Model the MCP server's separate task created during aopen.
                self.requests = asyncio.Queue()
                async def serve():
                    while True:
                        name, args, result = await self.requests.get()
                        try:
                            result.set_result(await invoke(registry[name], **args))
                        except Exception as exc:
                            result.set_exception(exc)
                self.worker = asyncio.create_task(serve())

            async def run(self, prompt):
                async def call(name, **args):
                    result = asyncio.get_running_loop().create_future()
                    await self.requests.put((name, args, result))
                    return await result
                return await action(call, selected, prompt)

            async def aclose(self):
                self.worker.cancel()
                try:
                    await self.worker
                except asyncio.CancelledError:
                    pass

        return Runtime()

    result = await schedule_runner.run_task(hub, declaration, identity=who if isinstance(who, RunIdentity) else ident(who),
                                            runtime_factory=factory, capture=False)
    assert result.result == "done", result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [ALICE, RunIdentity("workflow:md:daily", None, "legacy-service")])
async def test_progress_and_notes_survive_rounds_and_a_fresh_run(hub, who):
    calls = 0
    async def action(call, selected, prompt):
        nonlocal calls
        state = f"{selected.scratch_rel}/state.json"
        notes = f"{selected.scratch_rel}/notes/next.md"
        if calls == 0:
            assert "not found" in await call("read_file", path=state)
        else:
            assert json.loads(await call("read_file", path=state)) == {"completed": calls}
            assert "continue here" in await call("read_file", path=notes)
        calls += 1
        assert "wrote" in await call("write_hub_file", path=state, content=json.dumps({"completed": calls}))
        assert "wrote" in await call("write_hub_file", path=notes, content="continue here")
        assert notes in await call("list_files", glob=f"{selected.scratch_rel}/**/*")
        assert "continue here" in await call("grep_data", pattern="continue", path=selected.scratch_rel)
        return "STATUS: CONTINUE" if calls == 1 else "STATUS: DONE"

    await run(hub, task(max_rounds=2), who, action)
    await run(hub, task(), who, action)
    assert calls == 3
    identity = who if isinstance(who, RunIdentity) else ident(who)
    state = f"{markdown_scratch(hub, 'daily', identity)}/state.json"
    assert "refused" in await invoke(builtins(hub)["read_file"], path=state)


@pytest.mark.asyncio
async def test_parallel_people_and_jobs_do_not_share_scope(hub):
    pairs = [("daily", ALICE), ("daily", BOB), ("weekly", ALICE)]
    paths = [f"{markdown_scratch(hub, name, ident(who))}/state.json" for name, who in pairs]
    for n, path in enumerate(paths):
        p = hub / path
        p.parent.mkdir(parents=True)
        p.write_text(f"state-{n}")
    ready = asyncio.Event()
    entered = 0

    async def action(call, selected, prompt):
        nonlocal entered
        entered += 1
        if entered == len(pairs):
            ready.set()
        await ready.wait()
        own = f"{selected.scratch_rel}/state.json"
        for path in paths:
            result = await call("read_file", path=path)
            assert ("refused" not in result) == (path == own)
        # A nested ordinary chat must not borrow the scheduled scope either.
        with _request_ctx.chat_scope("ordinary-chat"):
            assert "refused" in await invoke(builtins(hub)["read_file"], path=own)
        return "STATUS: DONE"

    await asyncio.gather(*(run(hub, task(name), who, action) for name, who in pairs))
    for path in paths:
        assert "refused" in await invoke(builtins(hub)["read_file"], path=path)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [".env", "tokens.env", "data.sqlite", "restricted/note.txt", "renamed.txt"])
async def test_private_content_stays_denied_inside_own_scratch(hub, private):
    scratch = hub / markdown_scratch(hub, "daily", ident(ALICE))
    target = scratch / private
    target.parent.mkdir(parents=True)
    target.write_bytes(b"SQLite format 3\x00contents" if private == "renamed.txt" else b"private-marker")

    async def action(call, selected, prompt):
        path = str(target.relative_to(hub))
        assert "refused" in await call("read_file", path=path)
        assert path not in await call("list_files", glob=f"{selected.scratch_rel}/**/*")
        assert "private-marker" not in await call("grep_data", pattern="private-marker", path=selected.scratch_rel)
        return "STATUS: DONE"
    await run(hub, task(write=["."]), ALICE, action)


@pytest.mark.asyncio
async def test_symlink_and_traversal_cannot_escape_scope(hub):
    scratch = hub / markdown_scratch(hub, "daily", ident(ALICE))
    other = hub / markdown_scratch(hub, "daily", ident(BOB))
    scratch.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "state.json").write_text("private-marker")
    (scratch / "link.json").symlink_to(other / "state.json")
    (scratch / "linked-dir").symlink_to(other, target_is_directory=True)
    paths = [scratch / "link.json", scratch / "linked-dir" / "state.json",
             scratch / ".." / other.name / "state.json"]

    async def action(call, selected, prompt):
        for path in paths:
            assert "refused" in await call("read_file", path=str(path.relative_to(hub)))
        assert "private-marker" not in await call("grep_data", pattern="private-marker", path=selected.scratch_rel)
        return "STATUS: DONE"
    await run(hub, task(), ALICE, action)


@pytest.mark.asyncio
async def test_a_symlinked_scope_root_is_not_an_access_grant(hub):
    scratch = hub / markdown_scratch(hub, "daily", ident(ALICE))
    other = hub / markdown_scratch(hub, "daily", ident(BOB))
    other.mkdir(parents=True)
    (other / "state.json").write_text("private-marker")
    scratch.symlink_to(other, target_is_directory=True)

    async def action(call, selected, prompt):
        assert "refused" in await call("read_file", path=f"{selected.scratch_rel}/state.json")
        return "STATUS: DONE"
    await run(hub, task(), ALICE, action)


@pytest.mark.asyncio
async def test_scope_resets_on_cancellation(hub):
    path = f"{markdown_scratch(hub, 'daily', ident(ALICE))}/state.json"
    async def action(call, selected, prompt):
        await call("write_hub_file", path=path, content="saved")
        assert await call("read_file", path=path) == "saved"
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run(hub, task(), ALICE, action)
    assert "refused" in await invoke(builtins(hub)["read_file"], path=path)


@pytest.mark.asyncio
async def test_scope_cannot_be_reused_by_another_hubs_tools(hub, tmp_path):
    other_hub = tmp_path / "other-hub"
    other_hub.mkdir()
    rel = f"{markdown_scratch(hub, 'daily', ident(ALICE))}/state.json"
    target = other_hub / rel
    target.parent.mkdir(parents=True)
    target.write_text("other-hub-private")

    async def action(call, selected, prompt):
        assert "refused" in await invoke(builtins(other_hub)["read_file"], path=rel)
        return "STATUS: DONE"
    await run(hub, task(), ALICE, action)
