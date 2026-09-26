"""Markdown tasks that opt in to publish/email, driven through both tool
adapters (OpenAI Agents FunctionTool and the Claude SDK wrapper). Model-free: a
scripted runtime calls the tools the way an agent would.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from hubzoid import artifacts as arts
from hubzoid import schedule_runner as runner
from hubzoid import scheduling as sch
from hubzoid.access import store_for
from hubzoid.workflows.identity import RunIdentity

ALICE = "alice@company.com"


@pytest.fixture
def team(tmp_path, monkeypatch):
    for k in ("HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "team"
    (d / "schedule").mkdir(parents=True)
    (d / "AGENTS.md").write_text("---\nname: team\n---\nbody")
    for who in (ALICE, "bob@company.com"):
        store_for(d).upsert_identity(email=who, owui_id="id-" + who)
    return d


def _task(hub_dir, *, opt_in=True, run_as=ALICE):
    extra = "publish_artifacts: true\nsend_email: true\n" if opt_in else ""
    (hub_dir / "schedule" / "digest.md").write_text(
        f"---\nschedule: \"0 7 * * *\"\nrun_as: {run_as}\n{extra}---\nWrite the digest.\n")
    tasks, problems = sch.load_tasks(hub_dir)
    assert not problems, problems
    return tasks[0]


async def _via_openai(ft, args):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    raw = json.dumps(args)
    ctx = ToolContext(context=None, tool_name=ft.name, tool_call_id="t1",
                      tool_arguments=raw, run_config=RunConfig())
    return await ft.on_invoke_tool(ctx, raw)


async def _via_claude(ft, args):
    from hubzoid.factory_claude import _to_claude_tool

    out = await _to_claude_tool(ft).handler(args)
    return out["content"][0]["text"]


class _ScriptedAgent:
    """Does what a model would: write a report, publish it, email a link."""

    def __init__(self, tools, invoke):
        self.tools = {t.name: t for t in tools}
        self.invoke = invoke
        self.seen = {}

    async def run(self, prompt):
        scratch = re.search(r"keep your progress in (\S+)/state\.json", prompt).group(1)
        self.seen["names"] = sorted(self.tools)
        await self.invoke(self.tools["write_hub_file"],
                          {"path": f"{scratch}/digest.html", "content": "<h1>Digest</h1>"})
        self.seen["outside"] = await self.invoke(self.tools["publish_artifact"],
                                                 {"path": "AGENTS.md", "title": "x"})
        published = await self.invoke(self.tools["publish_artifact"],
                                      {"path": f"{scratch}/digest.html", "title": "Digest"})
        self.seen["published"] = published
        aid = re.search(r"report (a[\w-]+):", published).group(1)
        self.seen["mail"] = await self.invoke(self.tools["send_email"], {
            "subject": "Your digest", "body": "Ready.", "artifact_ids": [aid]})
        self.seen["artifact"] = aid
        return "Done.\nSTATUS: DONE — digest published"


@pytest.mark.parametrize("invoke", [_via_openai, _via_claude], ids=["openai", "claude"])
def test_opted_in_task_publishes_and_emails_as_its_person(team, invoke):
    from hubzoid.tools import schedule_tools

    task = _task(team)
    agents = []

    def factory(hub_dir, t, emit):
        agent = _ScriptedAgent(schedule_tools.make(hub_dir, t, emit), invoke)
        agents.append(agent)
        return agent

    result = asyncio.run(runner.run_task(team, task, runtime_factory=factory, capture=False))
    assert result.result == "done", result.error
    seen = agents[0].seen
    assert {"publish_artifact", "send_email"} <= set(seen["names"])
    assert "refused" in seen["outside"]                        # only its writable paths
    assert arts.get(team, seen["artifact"]).owner == ALICE
    assert seen["mail"].startswith("[previewed]") and "no email was sent" in seen["mail"]
    outbox = next((team / ".hubzoid" / "outbox").iterdir())
    assert outbox.name.startswith("alice")


def test_tasks_that_do_not_opt_in_get_no_new_tools(team):
    from hubzoid.tools import schedule_tools

    task = _task(team, opt_in=False)
    task.run_identity = RunIdentity(ALICE, "id", "run_as", ALICE).to_dict()
    names = {t.name for t in schedule_tools.make(team, task, lambda **_: None)}
    assert names == {"run_git", "write_hub_file"}


def test_script_tasks_cannot_opt_in(team):
    (team / "schedule" / "x.md").write_text(
        "---\nschedule: \"0 1 * * *\"\nrun: echo hi\nsend_email: true\n---\n")
    _, problems = sch.load_tasks(team)
    assert problems and "script" in problems[0]


def test_the_tools_have_the_same_schema_for_every_runtime(team):
    from hubzoid.factory_claude import _to_claude_tool
    from hubzoid.tools import schedule_tools

    task = _task(team)
    task.run_identity = RunIdentity(ALICE, "id", "run_as", ALICE).to_dict()
    for ft in schedule_tools.make(team, task, lambda **_: None):
        wrapped = _to_claude_tool(ft)
        assert wrapped.name == ft.name and wrapped.input_schema == ft.params_json_schema
