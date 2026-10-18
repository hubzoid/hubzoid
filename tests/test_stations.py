"""Isolated agents (stations) and handoffs, without a model.

The engine-backed journey with real Claude and Codex runs is covered by the
milestone's test-hub run. These cover definitions, isolation, the one retry,
crash-recovery accounting and handoff scope, with the runtime replaced.
"""
from __future__ import annotations

import json
import pickle
import textwrap

import pytest

from hubzoid import db
from hubzoid.stations import StationError, StationOutputError, definitions
from hubzoid.stations import service as stations
from hubzoid.work_items import service, store
from hubzoid.work_items.errors import WorkItemError
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity

OWNER = "admin@localhost"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}

WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"plan": Route(stages=("analysis", "review"), actions=())})
    def desk():
        return hub.call_agent("x", agent="analyst", stage="analysis")
''')


def station_file(name, *, tools="[]", extra="", body="Do the analysis."):
    return f"---\nname: {name}\ndescription: {name} station\nisolated: true\nengine: claude-local\n" \
           f"tools: {tools}\nresponse_schema: schemas/result.json\n{extra}---\n{body}\n"


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for var in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "desk"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\nmodel: claude-local\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    for name, tools in (("analyst", "[save_handoff]"), ("reviewer", "[read_handoff]")):
        folder = root / "agents" / name
        (folder / "schemas").mkdir(parents=True)
        (folder / "AGENTS.md").write_text(station_file(name, tools=tools))
        (folder / "schemas" / "result.json").write_text(json.dumps(SCHEMA))
    (root / "agents" / "helper.md").write_text("---\nname: helper\nmodel: claude-local/opus\n---\nA skill.\n")
    return root


def run_ctx(hub_dir, item):
    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    return context.run_scope(hub="desk", workflow="desk", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                             identity=ident, run_id="run-1", work={"item": item, "stage": None})


class FakeRuntime:
    """Stands in for runtime.run_station and records what each run received."""

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, hub_dir, definition, message, *, subject=None, timeout=None):
        self.calls.append({"definition": definition, "message": message, "subject": subject})
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return {"text": json.dumps(out), "output": out, "model": "fake", "usage": {}}


@pytest.fixture
def fake(monkeypatch):
    from hubzoid import runtime

    def install(*outputs):
        rt = FakeRuntime(*outputs)
        monkeypatch.setattr(runtime, "run_station", rt)
        return rt

    return install


def item_for(hub_dir, key="k"):
    return service.admit(hub_dir, workflow="desk", key=key, requester=OWNER)[0]


def test_isolated_agents_are_never_skills(hub_dir):
    from hubzoid.loaders import agents as agents_loader

    skills, delegates = agents_loader.split_subagents(hub_dir, "claude-local")
    names = {a.spec.name for a in skills + delegates}
    assert "analyst" not in names and "reviewer" not in names and "helper" in names
    assert set(definitions.load_all(hub_dir)) == {"analyst", "reviewer"}


def test_validation_and_digest(hub_dir):
    report = definitions.validate(hub_dir)
    assert report["valid"], report["checks"]
    before = definitions.load(hub_dir, "analyst").digest
    (hub_dir / "agents" / "analyst" / "AGENTS.md").write_text(station_file("analyst", tools="[save_handoff]",
                                                                           body="Name the owner too."))
    assert definitions.load(hub_dir, "analyst").digest != before        # a change is a new definition
    (hub_dir / "agents" / "analyst" / "AGENTS.md").write_text(station_file("analyst", tools="[shell_everything]"))
    report = definitions.validate(hub_dir)
    assert not report["valid"] and any(c["code"] == "unknown_tool" for c in report["checks"])
    (hub_dir / "agents" / "analyst" / "AGENTS.md").write_text(station_file("analyst", extra="engine: gpt-local\n"))
    with pytest.raises(WorkItemError) as err:
        definitions.load(hub_dir, "analyst")
    assert err.value.code == "invalid_station_definition"


def test_empty_tools_means_no_tools(hub_dir):
    from hubzoid.stations import tools

    assert tools.for_station(hub_dir, []) == {}
    assert list(tools.for_station(hub_dir, ["save_handoff"])) == ["save_handoff"]
    with pytest.raises(WorkItemError):
        tools.for_station(hub_dir, ["no_such_tool"])


def test_claude_station_gets_only_its_tools(monkeypatch):
    """No Claude Code built-ins, no settings, no host MCP servers, native
    structured output."""
    import asyncio

    from hubzoid import factory_claude

    seen = {}

    async def fake_query(*, prompt, options, hub_dir=None):
        seen["options"], seen["prompt"] = options, prompt
        from claude_agent_sdk import ResultMessage
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", result="{}", structured_output={"summary": "ok"}, usage={})

    monkeypatch.setattr(factory_claude, "_query_with_failover", fake_query)
    text, structured, _ = asyncio.run(factory_claude.claude_station(
        "task", instructions="Only this.", registry={}, schema=SCHEMA, model_setting="claude-local/haiku"))
    options = seen["options"]
    assert structured == {"summary": "ok"} and seen["prompt"] == "task"
    assert options.tools == [] and options.allowed_tools == [] and options.mcp_servers == {}
    assert options.setting_sources == [] and options.system_prompt == "Only this."
    assert options.output_format == {"type": "json_schema", "schema": SCHEMA}


def test_call_saves_once_and_conflicts(hub_dir, fake):
    rt = fake({"summary": "fine"})
    item = item_for(hub_dir)
    with run_ctx(hub_dir, item):
        assert stations.call("analyst", "Analyse this.", stage="analysis") == {"summary": "fine"}
        # The same call again returns the saved result, with no second run.
        assert stations.call("analyst", "Analyse this.", stage="analysis") == {"summary": "fine"}
        with pytest.raises(StationError) as err:
            stations.call("analyst", "Something else.", stage="analysis")
        assert err.value.code == "call_conflict"
        with pytest.raises(WorkItemError) as err:
            stations.call("analyst", "x", stage="publish")
        assert err.value.code == "scope_exceeded"
    assert len(rt.calls) == 1
    assert rt.calls[0]["definition"]["tools"] == ["save_handoff"] and rt.calls[0]["subject"] == OWNER
    row = stations.list_calls(hub_dir, item["id"])[0]
    assert row["status"] == "completed" and row["definition_digest"].startswith("sha256:")


def test_one_orchestrator_retry(hub_dir, fake):
    rt = fake({"wrong": 1}, {"summary": "fixed"}, {"wrong": 2})
    item = item_for(hub_dir)
    with run_ctx(hub_dir, item):
        with pytest.raises(StationOutputError) as err:
            stations.call("analyst", "Analyse this.", stage="analysis")
        call_id = err.value.call_id
        assert stations.retry(call_id, "Return only summary.") == {"summary": "fixed"}
        assert "Return only summary." in rt.calls[1]["message"]
    row = stations.get_call(hub_dir, call_id)
    assert row["retry_used"] == 1 and [a["kind"] for a in row["attempts_json"]] == ["initial", "retry"]
    # A second failure cannot be retried again.
    item2 = item_for(hub_dir, "k2")
    rt.outputs[:] = [{"wrong": 3}, {"wrong": 4}]
    with run_ctx(hub_dir, item2):
        with pytest.raises(StationOutputError) as err:
            stations.call("analyst", "Analyse.", stage="analysis")
        with pytest.raises(StationOutputError):
            stations.retry(err.value.call_id, "Try again.")
        with pytest.raises(StationError) as again:
            stations.retry(err.value.call_id, "Once more.")
    assert again.value.code == "retry_exhausted"


def test_retry_can_be_turned_off(hub_dir, fake):
    fake({"wrong": 1})
    (hub_dir / "work-items.yaml").write_text("version: 1\nwork_items:\n  stations:\n    orchestrator_retries: 0\n")
    item = item_for(hub_dir)
    with run_ctx(hub_dir, item):
        with pytest.raises(StationOutputError) as err:
            stations.call("analyst", "Analyse.", stage="analysis")
        with pytest.raises(StationError) as again:
            stations.retry(err.value.call_id, "Again.")
    assert again.value.code == "retry_exhausted"


def test_crash_recovery_is_counted_separately(hub_dir, fake):
    rt = fake({"summary": "recovered"})
    item = item_for(hub_dir)
    # A process died during the first attempt: its attempt is still `running`.
    with store.engine(hub_dir).begin() as conn:
        from sqlalchemy import text
        conn.execute(text(
            "INSERT INTO hz_station_calls (id, hub, item_id, generation, stage, revision_no, agent, definition_digest, "
            "definition_json, message_digest, message, status, attempts_json, revision, created, updated) VALUES "
            "('c1', 'desk', :i, 1, 'analysis', 1, 'analyst', 'd', :dj, :md, 'Analyse.', 'started', :att, 1, 0, 0)"),
            {"i": item["id"], "dj": json.dumps(definitions.load(hub_dir, "analyst").to_json()),
             "md": "sha256:" + __import__("hashlib").sha256(b"Analyse.").hexdigest(),
             "att": json.dumps([{"n": 1, "kind": "initial", "status": "running", "started": 0}])})
    with run_ctx(hub_dir, item):
        assert stations.call("analyst", "Analyse.", stage="analysis") == {"summary": "recovered"}
    row = stations.get_call(hub_dir, "c1")
    assert row["recovery_count"] == 1 and row["retry_used"] == 0          # recovery spends no retry
    assert [a["kind"] + ":" + a["status"] for a in row["attempts_json"]] == ["initial:interrupted",
                                                                               "recovery:completed"]
    assert len(rt.calls) == 1


def test_handoffs_are_scoped_and_replayable(hub_dir):
    item, other = item_for(hub_dir, "a"), item_for(hub_dir, "b")
    first = stations.save_handoff(hub_dir, item, "analysis", "Policy check", "The policy names no owner.")
    again = stations.save_handoff(hub_dir, item, "analysis", "Policy check", "The policy names no owner.")
    assert first["id"] == again["id"] and first["sha256"].startswith("sha256:")
    assert stations.read_handoff(hub_dir, item["id"], first["id"])["markdown"] == "The policy names no owner."
    with pytest.raises(WorkItemError) as err:          # another work item cannot read it
        stations.read_handoff(hub_dir, other["id"], first["id"])
    assert err.value.status == 404
    (hub_dir / "work-items.yaml").write_text(
        "version: 1\nwork_items:\n  handoffs:\n    kinds: [analysis]\n    max_characters: 10\n")
    with pytest.raises(WorkItemError):
        stations.save_handoff(hub_dir, item, "gossip", "x", "short")
    with pytest.raises(WorkItemError) as err:
        stations.save_handoff(hub_dir, item, "analysis", "x", "far too long for the limit")
    assert err.value.code == "payload_too_large"


def test_views_follow_the_work_item(hub_dir, fake):
    fake({"summary": "fine"})
    item = item_for(hub_dir)
    with run_ctx(hub_dir, item):
        stations.call("analyst", "Analyse.", stage="analysis")
    stations.save_handoff(hub_dir, item, "analysis", "Title", "Body")
    mine = stations.view_for(hub_dir, item["id"], OWNER)
    assert mine["calls"][0]["output"] == {"summary": "fine"} and mine["handoffs"][0]["title"] == "Title"
    with pytest.raises(WorkItemError):
        stations.view_for(hub_dir, item["id"], "stranger@company.org")


def test_station_errors_survive_the_engine():
    err = pickle.loads(pickle.dumps(StationOutputError("station_output_error", "bad shape", "call-1",
                                                       problems=["summary: required"])))
    assert isinstance(err, StationOutputError) and err.call_id == "call-1" and err.code == "station_output_error"
