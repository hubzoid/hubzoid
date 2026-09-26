"""Personal (Open WebUI native MCP) connections reach a workflow run as the run's
account, and only that account, on the `workflow` surface.

The full path, not just the surface list: a workflow's `hub.call_agent` goes
through the agent seam to `runtime.run_once`, which builds the hub's real
runtime and binds the run's account on the `workflow` surface; the runtime's
per-turn injection (`owui_mcp`) picks that person's Open WebUI session, checks
their `connector_<app>` grant, and connects to a real MCP HTTP server that
answers only the bearers it knows and says whom it thinks it is serving.

Two synthetic people, alice and bob, each connect the same `mail` server with
their own token. No model is called: the agent step is a probe that calls the
tools it was given.
"""
from __future__ import annotations

import json

import pytest
from agents import RunConfig, RunContextWrapper
from agents.tool_context import ToolContext

from hubzoid import owui_mcp
from hubzoid.access import Identity, identity_scope
from hubzoid.workflows import context as wctx
from hubzoid.workflows.context import hub as wf_hub, run_scope
from hubzoid.workflows.identity import IdentityError, resolve
from tests import connect_helpers as h

ALICE, BOB = "alice@example.org", "bob@example.org"
SECRET = "workflow-mcp-secret"


@pytest.fixture(scope="module")
def mail():
    bearers = {"tok-alice": ALICE, "tok-bob": BOB}
    with h.mcp_server("mail", bearers, {"whoami": lambda who: who or "nobody",
                                        "mail_search": lambda who: f"mail of {who}"}) as url:
        yield url


@pytest.fixture
def team(tmp_path, monkeypatch, mail):
    hub_dir = tmp_path / "team"
    hub_dir.mkdir()
    (hub_dir / "AGENTS.md").write_text("---\nname: team\ndescription: test hub\n---\nHelp.\n")
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ua", ALICE), ("ub", BOB)], secret=SECRET,
                servers=[{"id": "mail", "name": "Mail", "url": mail}])
    h.connect(db, user_id="ua", server_id="mail", secret=SECRET, access_token="tok-alice")
    h.connect(db, user_id="ub", server_id="mail", secret=SECRET, access_token="tok-bob")
    h.owui_env(monkeypatch, db, SECRET)
    eng = h.isolated_store(tmp_path, monkeypatch)
    for k in ("HUBZOID_RESTRICTED_SURFACES", "HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    # A Console-managed hub: each person holds Use this agent + connector_mail.
    import hubzoid.access as access

    gs = access.store_for(hub_dir)
    gs.set_authoritative(True, hub=hub_dir.name)
    for who, uid in ((ALICE, "ua"), (BOB, "ub")):
        gs.upsert_identity(email=who, owui_id=uid)
        gs.grant(who, hub_dir.name, owui_mcp.capability("mail"), actor="test")
    # The agent seam exactly as the bridge wires it (server.py).
    from hubzoid import runtime as agent_rt

    monkeypatch.setattr(wctx, "_AGENT", lambda task, hub_dir=None, subject=None:
                        agent_rt.run_once(hub_dir, task, subject=subject))
    monkeypatch.setattr(wctx, "_AGENT_STEP", None)
    return hub_dir, gs, db, eng


class _Probe:
    """Stands in for Runner.run_streamed: records which personal servers the
    turn was given and whom each MCP call was served as."""

    def __init__(self):
        self.servers, self.seen, self.surfaces = [], [], []

    def __call__(self, agent, run_input, max_turns=None):  # noqa: ARG002
        probe = self
        from hubzoid.access import current_identity

        class Result:
            final_output = "ok"

            async def stream_events(self):
                probe.surfaces.append(current_identity().surface)
                probe.servers.append(sorted(s.name for s in agent.mcp_servers))
                for t in await agent.get_mcp_tools(RunContextWrapper(context=None)):
                    if t.name == "whoami":
                        ctx = ToolContext(context=None, tool_name=t.name, tool_call_id="c1",
                                          tool_arguments="{}", run_config=RunConfig())
                        out = await t.on_invoke_tool(ctx, "{}")
                        probe.seen.append(_text(out))
                if False:  # pragma: no cover - makes this an async generator
                    yield None

        return Result()


def _text(result) -> str:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return result
    if isinstance(data, dict):
        data = data.get("text") or data
    if isinstance(data, list):
        return "".join(b.get("text", "") for b in data if isinstance(b, dict))
    return str(data)


def _run_as(hub_dir, who):
    """One workflow run acting as `who`, the way runtime.workflow binds it."""
    ident = resolve(hub_dir, hub=hub_dir.name, run_as=who, legacy_subject="workflow:digest")
    return run_scope(hub=hub_dir.name, workflow="digest", hub_dir=hub_dir, engine=None,
                     identity=ident.to_dict(), run_id="run-1")


@pytest.fixture
def probe(monkeypatch):
    import agents

    p = _Probe()
    monkeypatch.setattr(agents.Runner, "run_streamed", p)
    return p


def test_each_run_reaches_only_its_own_account(team, probe):
    hub_dir, *_ = team
    with _run_as(hub_dir, ALICE):
        wf_hub.call_agent("check my mail")
    with _run_as(hub_dir, BOB):
        wf_hub.call_agent("check my mail")
    assert probe.surfaces == ["workflow", "workflow"]
    assert probe.servers == [["owui_mail"], ["owui_mail"]]
    # The MCP server saw alice's own token in her run and bob's in his.
    assert probe.seen == [ALICE, BOB]


def test_revoking_the_connector_grant_denies_the_next_run(team, probe):
    hub_dir, gs, *_ = team
    with _run_as(hub_dir, ALICE):
        wf_hub.call_agent("check my mail")
        gs.revoke(ALICE, hub_dir.name, owui_mcp.capability("mail"), actor="admin@example.org")
        wf_hub.call_agent("check my mail")                        # same run, next call
    assert probe.servers == [["owui_mail"], []]
    assert probe.seen == [ALICE]
    with _run_as(hub_dir, BOB):                                   # bob is unaffected
        wf_hub.call_agent("check my mail")
    assert probe.seen == [ALICE, BOB]


def test_a_disconnected_session_is_not_used(team, probe):
    import sqlite3

    hub_dir, _, db, _ = team
    con = sqlite3.connect(db)
    con.execute("DELETE FROM oauth_session WHERE user_id='ua'")   # alice disconnected
    con.commit()
    con.close()
    with _run_as(hub_dir, ALICE):
        wf_hub.call_agent("check my mail")
    with _run_as(hub_dir, BOB):
        wf_hub.call_agent("check my mail")
    assert probe.servers == [[], ["owui_mail"]]
    assert probe.seen == [BOB]                                    # never bob's for alice


def test_a_blocked_account_stops_before_any_agent_call(team, probe):
    hub_dir, gs, *_ = team
    with _run_as(hub_dir, ALICE):
        gs.suspend(ALICE, actor="admin@example.org")
        with pytest.raises(IdentityError):
            wf_hub.call_agent("check my mail")
    assert probe.servers == []


def test_the_legacy_service_identity_gets_no_personal_server(team, probe):
    hub_dir, gs, *_ = team
    gs.set_authoritative(False, hub=hub_dir.name)                 # a legacy hub
    with run_scope(hub=hub_dir.name, workflow="digest", hub_dir=hub_dir, engine=None):
        wf_hub.call_agent("check my mail")
    assert probe.servers == [[]]


@pytest.mark.asyncio
async def test_claude_and_codex_select_the_same_account_on_the_workflow_surface(team):
    """The other two runtimes read the same per-turn source; on the workflow
    surface it yields each person's own server and token, and nothing once the
    grant is revoked."""
    hub_dir, gs, *_ = team
    for who, token in ((ALICE, "tok-alice"), (BOB, "tok-bob")):
        with identity_scope(Identity.make(who, surface="workflow")):
            specs, allowed = owui_mcp.per_user_specs(hub_dir, Identity.make(who, surface="workflow"))
            servers = owui_mcp.per_user_servers(hub_dir, Identity.make(who, surface="workflow"))
        assert set(specs) == {"owui_mail"} and allowed == ["mcp__owui_mail__*"]
        assert specs["owui_mail"]["headers"]["Authorization"] == f"Bearer {token}"
        assert [s.headers["Authorization"] for s in servers] == [f"Bearer {token}"]
    gs.revoke(BOB, hub_dir.name, owui_mcp.capability("mail"), actor="admin@example.org")
    assert owui_mcp.per_user_specs(hub_dir, Identity.make(BOB, surface="workflow")) == ({}, [])
