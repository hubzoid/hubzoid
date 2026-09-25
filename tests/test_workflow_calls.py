"""Workflow model calls: `hub.call_llm` is one tool-free model call with text,
JSON and Pydantic modes on both backends; `hub.decide` calls Jev through
OpenRouter's decisions endpoint; every call and chat turn writes a usage row.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from hubzoid import runtime as runtime_lib
from hubzoid import structured, usage
from hubzoid.workflows import context as wctx
from hubzoid.workflows.context import hub, run_scope


class Ticket(BaseModel):
    team: str
    urgent: bool


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: sales\ndescription: d\n---\nbody")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    return d


def _rows(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    with eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_usage ORDER BY id"))]


# --- structured parsing -----------------------------------------------------
@pytest.mark.parametrize("reply", [
    '{"team": "payments", "urgent": true}',
    'Sure:\n```json\n{"team": "payments", "urgent": true}\n```',
    'Here you go {"team": "payments", "urgent": true} hope that helps',
])
def test_extract_json_is_tolerant(reply):
    assert structured.extract_json(reply) == {"team": "payments", "urgent": True}


def test_extract_json_refuses_prose():
    with pytest.raises(structured.ModelOutputError):
        structured.extract_json("I think payments should own it.")


# --- the hub proxy ------------------------------------------------------------
def _seam(reply_text):
    seen = []

    def llm(spec, **kw):
        seen.append(spec)
        want = spec["response_format"] == "json"
        return {"text": reply_text, "json": structured.extract_json(reply_text) if want else None}

    return llm, seen


def test_call_llm_modes(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    llm, seen = _seam('{"team": "payments", "urgent": true}')
    wctx.configure(llm=llm)
    try:
        with run_scope(hub="sales", workflow="w", hub_dir=tmp_path, engine=eng):
            assert hub.call_llm("route it").startswith("{")
            assert hub.call_llm("route it", response_format="json") == {"team": "payments", "urgent": True}
            t = hub.call_llm("route it", response_model=Ticket)
            assert isinstance(t, Ticket) and t.team == "payments"
        assert seen[2]["schema"]["title"] == "Ticket"  # the schema travels as plain data
        json.dumps(seen)  # every spec is serializable (DBOS checkpoints it)
    finally:
        wctx._LLM = None


def test_call_llm_schema_mismatch_is_a_clear_error(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    llm, _ = _seam('{"team": "payments"}')  # missing `urgent`
    wctx.configure(llm=llm)
    try:
        with run_scope(hub="sales", workflow="w", hub_dir=tmp_path, engine=eng):
            with pytest.raises(structured.ModelOutputError, match="Ticket"):
                hub.call_llm("route it", response_model=Ticket)
    finally:
        wctx._LLM = None


def test_call_agent_structured_answer(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    tasks = []
    wctx.configure(agent=lambda task, **kw: tasks.append(task) or
                   'Filed it.\n{"team": "account", "urgent": false}')
    try:
        with run_scope(hub="sales", workflow="w", hub_dir=tmp_path, engine=eng):
            t = hub.call_agent("file the ticket", response_model=Ticket)
        assert t == Ticket(team="account", urgent=False)
        assert "Finish your reply with a JSON object" in tasks[0]
    finally:
        wctx._AGENT = None


def test_decide_validates_question_types(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    wctx.configure(decide=lambda spec, **kw: {"answers": {}})
    try:
        with run_scope(hub="sales", workflow="w", hub_dir=tmp_path, engine=eng):
            with pytest.raises(ValueError, match="noul"):
                hub.decide({"t": "x"}, {"q": {"type": "essay"}})
    finally:
        wctx._DECIDE = None


# --- the backends -----------------------------------------------------------
def test_litellm_path_uses_json_mode_and_records_usage(hub_dir, tmp_path, monkeypatch):
    import litellm

    calls = []

    def fake_completion(**kw):
        calls.append(kw)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"team": "payments", "urgent": true}'))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=15),
        )

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0021)
    out = runtime_lib.complete_once(hub_dir, {
        "prompt": "route it", "system": None, "model": "openrouter/anthropic/claude-haiku-4.5",
        "response_format": "json", "schema": Ticket.model_json_schema()}, subject="workflow:triage")
    assert out["json"] == {"team": "payments", "urgent": True}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in calls[0]["messages"][-1]["content"]
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["surface"], row["subject"]) == ("llm", "workflow", "workflow:triage")
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (120, 15, 0.0021)


def test_claude_local_path_runs_one_turn_with_no_tools(hub_dir, tmp_path, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from claude_agent_sdk.types import TextBlock

    seen = {}

    async def fake_query(*, prompt, options):
        seen["options"] = options
        yield AssistantMessage(content=[TextBlock(text="Payments owns it.")], model="claude-sonnet-4-5")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s", usage={"input_tokens": 40, "output_tokens": 6},
                            model_usage={"claude-sonnet-4-5": {}})

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    out = runtime_lib.complete_once(hub_dir, {
        "prompt": "who owns checkout bugs?", "system": None, "model": "claude-local",
        "response_format": "text", "schema": None})
    assert out["text"] == "Payments owns it."
    opts = seen["options"]
    assert opts.tools == [] and opts.mcp_servers == {} and opts.max_turns == 1
    (row,) = _rows(tmp_path)
    assert row["model"] == "claude-sonnet-4-5" and row["input_tokens"] == 40
    assert row["cost_usd"] is not None  # estimated from the price table


def test_decide_once_posts_the_decisions_shape(hub_dir, tmp_path, monkeypatch):
    import httpx

    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.update(url=url, body=json, headers=headers)
        return httpx.Response(200, json={
            "model": "typesafe/jev-1.13-20260917",
            "answers": {"is_bug": {"type": "noul", "noul": 0.96}},
            "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
        })

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(httpx, "post", fake_post)
    questions = {"is_bug": {"type": "noul", "instructions": "Is this a defect?",
                            "criteria": {"true": "broken", "false": "question"}}}
    data = runtime_lib.decide_once(hub_dir, {"model": "typesafe/jev-1.13",
                                             "state": {"ticket": "blank page"},
                                             "questions": questions})
    assert sent["url"] == runtime_lib.DECISIONS_URL
    assert sent["body"] == {"model": "typesafe/jev-1.13", "state": {"ticket": "blank page"},
                            "questions": questions}
    assert sent["headers"]["Authorization"] == "Bearer sk-or-test"
    assert data["answers"]["is_bug"]["noul"] == 0.96
    (row,) = _rows(tmp_path)
    assert (row["kind"], row["cost_usd"], row["model"]) == ("decide", 0.000019992, "typesafe/jev-1.13-20260917")


def test_decide_needs_an_openrouter_key(hub_dir, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        runtime_lib.decide_once(hub_dir, {"model": "typesafe/jev-1.13", "state": {}, "questions": {}})


# --- usage rows ----------------------------------------------------------------
def test_cost_estimate_from_price_table():
    assert usage.estimate_cost("openrouter/anthropic/claude-haiku-4.5", 1000, 1000) == pytest.approx(0.006)
    assert usage.estimate_cost("not-a-real-model-xyz", 1000, 1000) is None


def test_record_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", "sqlite:////nonexistent/dir/ops.db")
    usage.record(tmp_path, hub="x", surface="web", kind="chat", subject=None)  # logs, no raise
