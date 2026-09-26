"""Markdown tasks that opt in to publish/email, driven through both tool
adapters (OpenAI Agents FunctionTool and the Claude SDK wrapper), and
`hub.connection` for Python workflows. Model-free: a scripted runtime calls the
tools the way an agent would.
"""
from __future__ import annotations

import asyncio
import json
import pickle
import re
from types import SimpleNamespace

import pytest

from hubzoid import artifacts as arts
from hubzoid import connections as conns
from hubzoid import schedule_runner as runner
from hubzoid import scheduling as sch
from hubzoid.access import store_for
from hubzoid.workflows import connection as wconn
from hubzoid.workflows.context import hub, run_scope
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


# --- hub.connection -------------------------------------------------------------------

class _Accounts:
    def __init__(self, rows):
        self.rows = rows

    def get(self, ref):
        return self.rows[ref]


class _Broker:
    """Composio-shaped fake: alice has one gmail account (or two)."""

    def __init__(self, accounts):
        self.accounts = accounts            # ref -> (user, status, token)
        rows = {ref: SimpleNamespace(user_id=u, status=s, toolkit=SimpleNamespace(slug="gmail"),
                                     state=SimpleNamespace(val={"token": t}))
                for ref, (u, s, t) in accounts.items()}
        self._client = SimpleNamespace(connected_accounts=_Accounts(rows))

    def active_account_ids(self, *, user, app):
        return [r for r, (u, s, _) in self.accounts.items() if u == user and s == "ACTIVE"]

    def get_credential(self, *, user, app):
        ids = self.active_account_ids(user=user, app=app)
        return {"token": self.accounts[ids[0]][2]} if ids else None

    def connect_link(self, *, user, app):
        return "https://connect.example/SECRET-LINK"

    def is_connected(self, *, user, app):
        return bool(self.active_account_ids(user=user, app=app))

    def execute(self, **kw):
        return {}


@pytest.fixture
def gate():
    yield lambda accounts: conns.set_gate(conns.Connections(client=_Broker(accounts),
                                                            allowed=["gmail"]))
    conns.set_gate(conns._INACTIVE)


def _as(team, who):
    ident = RunIdentity(who, "id-" + who, "run_as", who).to_dict()
    from sqlalchemy import create_engine

    return run_scope(hub="team", workflow="w", hub_dir=team, identity=ident,
                     engine=create_engine("sqlite://"))


def test_connection_is_the_run_persons_own(team, gate):
    gate({"ca_1": (ALICE, "ACTIVE", "alice-token"), "ca_2": ("bob@company.com", "ACTIVE", "bob-token")})
    with _as(team, ALICE):
        cred = hub.connection("gmail")
        assert cred["token"] == "alice-token"
        assert "alice-token" not in repr(cred) and "alice-token" not in str(cred)
        with pytest.raises(TypeError):
            pickle.dumps(cred)                                   # never checkpointed
        with pytest.raises(wconn.ConnectionFailed):
            hub.connection("gmail", ref="ca_2")                  # bob's account
        assert hub.connection("gmail", ref="ca_1")["token"] == "alice-token"


def test_several_connections_need_an_explicit_ref(team, gate):
    gate({"ca_1": (ALICE, "ACTIVE", "work"), "ca_3": (ALICE, "ACTIVE", "personal")})
    with _as(team, ALICE):
        with pytest.raises(wconn.ConnectionFailed, match="ref="):
            hub.connection("gmail")
        assert hub.connection("gmail", ref="ca_3")["token"] == "personal"


def test_missing_or_revoked_connection_fails_without_leaking_a_link(team, gate):
    gate({"ca_1": (ALICE, "EXPIRED", "old")})
    with _as(team, ALICE):
        with pytest.raises(wconn.ConnectionFailed) as err:
            hub.connection("gmail")
        assert "connect gmail" in str(err.value) and "SECRET-LINK" not in str(err.value)
        with pytest.raises(wconn.ConnectionFailed):
            hub.connection("gmail", ref="ca_1")
        with pytest.raises(wconn.ConnectionFailed):
            hub.connection("slack")                              # not offered by the hub


def test_a_legacy_service_run_has_no_personal_connection(team, gate):
    from sqlalchemy import create_engine

    from hubzoid.workflows.identity import IdentityError

    gate({"ca_1": (ALICE, "ACTIVE", "t")})
    with run_scope(hub="team", workflow="w", hub_dir=team, engine=create_engine("sqlite://")):
        with pytest.raises(IdentityError):
            hub.connection("gmail")
