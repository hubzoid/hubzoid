"""Evals in subfolders, fake tool results (discussion #62), JSON and JUnit
output (#58), and the doctor rows for required evals (quality/manifest.yaml)."""
from __future__ import annotations

import asyncio
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from agents.tool import FunctionTool
from typer.testing import CliRunner

from hubzoid import _request_ctx, tool_fakes
from hubzoid.evals import cases as cases_lib
from hubzoid.evals import junit, quality, report
from hubzoid.evals.results import CaseResult, SuiteResult
from hubzoid.evals.runner import arun_suite


def make_hub(tmp_path: Path, files: dict[str, str]) -> Path:
    hub = tmp_path / "desk"
    (hub / "evals").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: desk\n---\nYou are desk.\n")
    for name, text in files.items():
        path = hub / "evals" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return hub


def tool(name: str, calls: list) -> FunctionTool:
    async def invoke(_ctx, input_str):
        calls.append((name, input_str))
        return f"REAL {name}"

    return FunctionTool(name=name, description=name, params_json_schema={"type": "object", "properties": {}},
                        on_invoke_tool=invoke)


class ToolRuntime:
    """A runtime that calls hub tools the way the real ones do: through each
    tool's on_invoke_tool, after recording the call."""
    name = "tool-runtime"

    def __init__(self, registry: dict, script: dict):
        self.registry = {n: tool_fakes.wrap(t) for n, t in registry.items()}
        self.script = script

    async def aopen(self):
        pass

    async def aclose(self):
        pass

    async def run(self, prompt: str) -> str:
        out = []
        for name, args in self.script.get(prompt, []):
            _request_ctx.record_tool_call(name, args)
            out.append(await self.registry[name].on_invoke_tool(None, json.dumps(args)))
        return " | ".join(out) or "ok"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_cases_in_subfolders_get_path_ids_and_notes_are_skipped(tmp_path):
    hub = make_hub(tmp_path, {
        "refund.md": "Refund?",
        "approvals/reply-reject.md": "Reject?",
        "approvals/_draft.md": "draft",
        "_notes/idea.md": "idea",
        "fixtures/orders.md": "data",
        ".hidden/x.md": "x",
    })
    found = cases_lib.discover(hub)
    assert [c.name for c in found] == ["approvals/reply-reject", "refund"]
    assert all(c.digest.startswith("sha256:") for c in found)
    assert [c.name for c in cases_lib.select(found, case="approvals/*")] == ["approvals/reply-reject"]


def test_the_console_lists_nested_cases_and_names_a_broken_one(tmp_path):
    from hubzoid.portal_evals import read_cases

    hub = make_hub(tmp_path, {"safety/no-send.md": "Send?", "safety/bad.md": "---\ntimeout: soon\n---\nHi"})
    folder, found, errors = read_cases(hub)
    assert folder and [c.name for c in found] == ["safety/no-send"]
    assert errors == [{"file": "evals/safety/bad.md", "error": "safety/bad.md: timeout must be a whole number"}]


def test_two_ids_that_differ_only_in_case_are_refused(tmp_path):
    hub = make_hub(tmp_path, {"Refund.md": "a", "refund.md": "b"})
    if len(list((hub / "evals").iterdir())) < 2:
        pytest.skip("case-insensitive file system")
    with pytest.raises(cases_lib.EvalCaseError, match="same id"):
        cases_lib.discover(hub)


@pytest.mark.parametrize("front,why", [
    ("fake_tools: send_email", "list"),
    ("fake_tools:\n  - {tool: send_email}", "exactly one of output or error"),
    ("fake_tools:\n  - {tool: x, output: 1, colour: red}", "unknown key"),
    ("real_tools: [read_knowledge]", "only applies with fake_tools"),
    ('fake_tools:\n  - {tool: x, output: 1}\nallow_real_mcp: "false"', "true or false"),
    ("timeout: .inf", "whole number"),
])
def test_bad_fake_declarations_are_refused(tmp_path, front, why):
    hub = make_hub(tmp_path, {"c.md": f"---\n{front}\n---\nHi"})
    with pytest.raises(cases_lib.EvalCaseError, match=why):
        cases_lib.discover(hub)


# ---------------------------------------------------------------------------
# Fake tools
# ---------------------------------------------------------------------------
CASE = """---
fake_tools:
  - tool: lookup_order
    args: {order_id: A-1001}
    output: {status: cancelled}
  - tool: send_email
    error: SMTP is down
real_tools: [read_knowledge]
---
{prompt}
"""


def run(hub, rt, monkeypatch):
    monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: rt)
    return asyncio.run(arun_suite(hub, cases_lib.discover(hub), judge_fn=None))


def test_fakes_answer_matching_calls_and_block_everything_else(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, {"refund.md": CASE.replace("{prompt}", "Refund A-1001")})
    real: list = []
    rt = ToolRuntime({n: tool(n, real) for n in ("lookup_order", "send_email", "read_knowledge", "create_refund")},
                     {"Refund A-1001": [("lookup_order", {"order_id": "A-1001"}), ("send_email", {"to": "x"}),
                                        ("read_knowledge", {"name": "policy"}), ("lookup_order", {"order_id": "B-2"}),
                                        ("create_refund", {"amount": 49})]})
    suite = run(hub, rt, monkeypatch)
    case = suite.cases[0]
    assert real == [("read_knowledge", '{"name": "policy"}')]          # only the allowed real tool ran
    assert '{"status": "cancelled"} | Error: SMTP is down | REAL read_knowledge | Blocked in this eval' in case.response
    check = next(c for c in case.checks if c.kind == "fake_tools")
    assert not check.passed and "blocked 2 call(s)" in check.detail and "create_refund" in check.detail
    assert not case.passed
    assert [(t.name, t.ok) for t in case.tools] == [("lookup_order", None), ("send_email", None),
                                                    ("read_knowledge", None), ("lookup_order", False),
                                                    ("create_refund", False)]


def test_a_fully_faked_case_passes_and_outside_a_case_tools_run_for_real(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, {"refund.md": CASE.replace("{prompt}", "Look up A-1001")})
    real: list = []
    rt = ToolRuntime({"lookup_order": tool("lookup_order", real)},
                     {"Look up A-1001": [("lookup_order", {"order_id": "A-1001"})]})
    case = run(hub, rt, monkeypatch).cases[0]
    assert case.passed and real == []
    assert next(c for c in case.checks if c.kind == "fake_tools").detail == "1 call(s) answered by fakes"
    assert asyncio.run(rt.registry["lookup_order"].on_invoke_tool(None, "{}")) == "REAL lookup_order"


def test_the_fakes_check_names_the_connectors_left_out():
    from hubzoid.evals.runner import _fakes_check

    session = tool_fakes.Session(fakes=[], left_out=["mail"])
    assert _fakes_check(session).detail == "0 call(s) answered by fakes; the person's connectors (mail) were left out"


def test_a_faked_case_refuses_a_hub_with_mcp_servers_unless_allowed(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, {"refund.md": CASE.replace("{prompt}", "Hi")})
    (hub / "connectors").mkdir()
    (hub / "connectors" / ".mcp.json").write_text(json.dumps({"mcpServers": {"crm": {"url": "https://crm.example/mcp"}}}))
    rt = ToolRuntime({}, {})
    case = run(hub, rt, monkeypatch).cases[0]
    assert case.error and "MCP servers (crm)" in case.error and "allow_real_mcp" in case.error


def test_a_restricted_tools_access_check_runs_before_its_fake(tmp_path, monkeypatch):
    from hubzoid.access import guard

    real: list = []
    monkeypatch.setattr(guard, "decide", lambda *a, **k: (False, "no grant"))
    monkeypatch.setattr(guard.audit, "record", lambda *a, **k: True)
    guarded = guard.guard_tool(tool("erp_post", real), "erp", tmp_path)
    assert tool_fakes.wrap(guarded) is guarded  # not wrapped again in front of the check
    session = tool_fakes.Session(fakes=[tool_fakes.Fake(tool="erp_post", output="posted")])
    with tool_fakes.scope(session):
        out = asyncio.run(guarded.on_invoke_tool(None, "{}"))
    assert out.startswith("[access denied") and session.used == [] and real == []


def test_an_approval_layer_keeps_fakes_inside_it_and_its_added_tools_are_faked():
    real: list = []
    seen: list = []

    def gate(registry):              # wraps every tool again and adds a tool, as hitl.equip does
        def wrapped(ft):
            async def invoke(ctx, raw):
                seen.append(ft.name)
                return await ft.on_invoke_tool(ctx, raw)
            return FunctionTool(name=ft.name, description=ft.name, params_json_schema=ft.params_json_schema,
                                on_invoke_tool=invoke)
        return {**{n: wrapped(t) for n, t in registry.items()}, "ask_question": tool("ask_question", real)}

    before = tool_fakes.wrap_registry({"send_email": tool("send_email", real)})
    registry = tool_fakes.wrap_added(before, gate(before))
    session = tool_fakes.Session(fakes=[tool_fakes.Fake(tool="send_email", output="sent")])
    with tool_fakes.scope(session):
        assert asyncio.run(registry["send_email"].on_invoke_tool(None, "{}")) == "sent"
        assert asyncio.run(registry["ask_question"].on_invoke_tool(None, "{}")).startswith("Blocked in this eval")
    assert seen == ["send_email"] and real == []   # the gate ran first; nothing real ran


# ---------------------------------------------------------------------------
# JSON and JUnit
# ---------------------------------------------------------------------------
def suite_with(*results: CaseResult) -> SuiteResult:
    return SuiteResult(hub="desk", cases=list(results), started_at="2026-10-19T10:00:00+00:00", model="m")


def test_junit_marks_failures_and_errors():
    from hubzoid.evals.assertions import Check

    xml = junit.to_xml(suite_with(
        CaseResult(name="ok", duration=1.5),
        CaseResult(name="approvals/bad", checks=[Check(kind="contains", passed=False, detail="missing 'refund'")]),
        CaseResult(name="boom", error="timed out after 120s")))
    root = ET.fromstring(xml)
    suite = root.find("testsuite")
    assert suite.get("tests") == "3" and suite.get("failures") == "1" and suite.get("errors") == "1"
    cases = {c.get("name"): c for c in suite.findall("testcase")}
    assert cases["approvals/bad"].find("failure").text == "contains: missing 'refund'"
    assert cases["boom"].find("error").get("message") == "timed out after 120s"

    xml = junit.to_xml(suite_with(CaseResult(name="leak", error="\x1b[31mapi_key: sk-live-123\x1b[0m failed")))
    error = ET.fromstring(xml).find("testsuite/testcase/error")      # parses: no escape characters left
    assert "sk-live-123" not in xml.decode() and "[31m" in error.text


def test_eval_commands_print_json(tmp_path, monkeypatch):
    from hubzoid.cli import app

    hub = make_hub(tmp_path, {"refund.md": "---\ntags: [safety]\n---\nRefund?", "deep/one.md": "Hi"})
    runner = CliRunner()
    listed = json.loads(runner.invoke(app, ["eval", "list", str(hub), "--json"]).output)
    assert [c["id"] for c in listed["cases"]] == ["deep/one", "refund"]
    assert json.loads(runner.invoke(app, ["eval", "status", str(hub), "--json"]).output) == {"hub": "desk", "runs": 0}
    class Leaky(ToolRuntime):
        async def run(self, prompt):
            return "token: sk-live-123"

    monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: Leaky({}, {}))
    pushed = []
    monkeypatch.setattr("hubzoid.evals.langfuse.push", lambda hub_dir, suite: pushed.append(suite.passed))
    out = tmp_path / "ci" / "evals.xml"
    r = runner.invoke(app, ["eval", "run", str(hub), "--no-judge", "--json", "--junit", str(out)])
    body = json.loads(r.output)
    assert r.exit_code == 0 and body["passed"] == 2 and body["junit"] == str(out) and out.is_file()
    assert "sk-live-123" not in r.output and pushed == [2]
    status = json.loads(runner.invoke(app, ["eval", "status", str(hub), "--json"]).output)
    assert status["ok"] and status["total"] == 2
    explained = json.loads(runner.invoke(app, ["eval", "explain", str(hub), "deep/one", "--json"]).output)
    assert explained["name"] == "deep/one" and explained["stale"] is False


# ---------------------------------------------------------------------------
# Required evals in doctor
# ---------------------------------------------------------------------------
def test_doctor_reports_missing_failed_and_out_of_date_required_evals(tmp_path, monkeypatch):
    from hubzoid import doctor
    from hubzoid.evals.assertions import Check

    monkeypatch.delenv("MODEL", raising=False)

    hub = make_hub(tmp_path, {"refund.md": "Refund?", "safety/no-send.md": "---\ntags: [safety]\n---\nSend?",
                              "other.md": "Hi"})
    (hub / "quality").mkdir()
    (hub / "quality" / "manifest.yaml").write_text("version: 1\nrequired: [refund]\nrequired_tags: [safety]\n")
    rows = {c.id: c for c in doctor._eval_checks(hub)}
    assert set(rows) == {"eval.refund", "eval.safety/no-send"} and rows["eval.refund"].status == "pending"
    assert doctor.report(hub, list(rows.values()))["ok"] is False      # not run yet: not ready

    found = {c.name: c for c in cases_lib.discover(hub)}
    suite = suite_with(CaseResult(name="refund", case_digest=found["refund"].digest),
                       CaseResult(name="safety/no-send", case_digest=found["safety/no-send"].digest,
                                  checks=[Check(kind="forbid_tools", passed=False, detail="send_email called")]))
    suite.hub_digest = quality.hub_digest(hub)
    report.save(hub, suite)
    rows = {c.id: c for c in doctor._eval_checks(hub)}
    assert rows["eval.refund"].status == "ok"
    assert rows["eval.safety/no-send"].status == "fail" and "send_email called" in rows["eval.safety/no-send"].summary

    (hub / "evals" / "refund.md").write_text("Refund, please?")          # the case changed
    assert {c.id: c for c in doctor._eval_checks(hub)}["eval.refund"].status == "pending"
    (hub / "evals" / "refund.md").write_text("Refund?")
    (hub / ".env").write_text("OPENAI_API_KEY=sk-a\n")
    suite.hub_digest = quality.hub_digest(hub)
    report.save(hub, suite, stamp="20991231_235959")                     # the latest run
    (hub / ".env").write_text("OPENAI_API_KEY=sk-b\n")                # a rotated key changes nothing
    assert {c.id: c for c in doctor._eval_checks(hub)}["eval.refund"].status == "ok"
    (hub / ".env").write_text("OPENAI_API_KEY=sk-b\nMODEL=codex-local\n")  # another model: rerun
    assert {c.id: c for c in doctor._eval_checks(hub)}["eval.refund"].status == "pending"
    (hub / ".env").write_text("OPENAI_API_KEY=sk-b\n")
    monkeypatch.setenv("MODEL", "codex-local")                             # the same, from the shell
    assert {c.id: c for c in doctor._eval_checks(hub)}["eval.refund"].status == "pending"
    monkeypatch.delenv("MODEL")
    (hub / "AGENTS.md").write_text("---\nname: desk\n---\nNew rule: refunds need a receipt.\n")  # the hub changed
    row = {c.id: c for c in doctor._eval_checks(hub)}["eval.refund"]
    assert row.status == "pending" and "instructions" in row.summary


def test_a_judged_case_run_without_its_judge_stays_pending(tmp_path):
    from hubzoid import doctor

    hub = make_hub(tmp_path, {"tone.md": "# Prompt\nRefund?\n# Criteria\nPolite."})
    (hub / "quality").mkdir()
    (hub / "quality" / "manifest.yaml").write_text("version: 1\nrequired: [tone]\n")
    (case,) = cases_lib.discover(hub)
    suite = suite_with(CaseResult(name="tone", case_digest=case.digest))   # passed free checks, no judge
    suite.hub_digest = quality.hub_digest(hub)
    report.save(hub, suite)
    (row,) = doctor._eval_checks(hub)
    assert row.status == "pending" and "without its judge" in row.summary


def test_an_unreadable_required_case_fails_instead_of_dropping_out(tmp_path):
    from hubzoid import doctor

    hub = make_hub(tmp_path, {"safety/ok.md": "Hi", "safety/bad.md": "---\ntimeout: soon\n---\nHi"})
    (hub / "quality").mkdir()
    (hub / "quality" / "manifest.yaml").write_text("version: 1\nrequired: [safety/*]\n")
    rows = {c.id: c for c in doctor._eval_checks(hub)}
    assert rows["eval.safety/bad"].status == "fail" and "cannot be read" in rows["eval.safety/bad"].summary
    assert rows["eval.safety/ok"].status == "pending"


def test_the_hub_digest_covers_every_byte_of_large_files(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, {})
    (hub / "knowledge").mkdir()
    big = hub / "knowledge" / "manual.md"
    big.write_bytes(b"a" * (3 * 1024 * 1024))
    before = quality.hub_digest(hub)
    big.write_bytes(b"a" * (3 * 1024 * 1024 - 1) + b"b")                 # same size, one byte changed
    assert quality.hub_digest(hub) != before
    monkeypatch.setattr(quality, "_MAX_FILES", 0)
    assert quality.hub_digest(hub) == quality.INCOMPLETE


def test_a_requirement_that_matches_nothing_fails(tmp_path, monkeypatch):
    from hubzoid import doctor

    hub = make_hub(tmp_path, {"refund.md": "Refund?"})
    (hub / "quality").mkdir()
    (hub / "quality" / "manifest.yaml").write_text("version: 1\nrequired: [refnud]\n")
    (row,) = doctor._eval_checks(hub)
    assert row.id == "evals.quality" and row.status == "fail" and "refnud" in row.summary
    assert doctor._eval_checks(make_hub(tmp_path / "b", {"x.md": "Hi"})) == []
    monkeypatch.setattr(quality, "evidence", lambda hub_dir: 1 / 0)       # unreadable results never pass
    assert [(c.id, c.status) for c in doctor._eval_checks(hub)] == [("evals.quality", "fail")]
