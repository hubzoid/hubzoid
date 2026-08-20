"""Tests for the eval format, the free-tier assertions, and the runner.

The judge tier lives in tests/test_evals_judge.py, scheduling in
tests/test_evals_schedule.py, and a real-LLM walkthrough in
tests/e2e/test_evals_e2e.py. Everything here runs with no model and no
network: the runner is exercised against a stubbed Runtime, which is the
whole reason `arun_suite` takes an injected judge instead of importing one.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hubzoid import _request_ctx, tool_events
from hubzoid.evals import assertions, cases, report, runner
from hubzoid.evals.results import CaseResult, JudgeResult, SuiteResult


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _hub(tmp_path: Path, files: dict[str, str]) -> Path:
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    for name, text in files.items():
        (hub / "evals" / name).write_text(text, encoding="utf-8")
    return hub


class FakeRuntime:
    """Stands in for a built Runtime. Records prompts, replays scripted text."""

    name = "fake-model"

    def __init__(self, replies=None, tools=None, delay=0.0, raises=None):
        self._replies = replies or {}
        self._tools = tools or {}
        self._delay = delay
        self._raises = raises
        self.prompts: list[str] = []
        self.opened = self.closed = 0

    async def aopen(self):
        self.opened += 1

    async def aclose(self):
        self.closed += 1

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self._raises:
            raise self._raises
        if self._delay:
            await asyncio.sleep(self._delay)
        for name in self._tools.get(prompt, []):
            _request_ctx.record_tool_call(name, {"q": "x"})
        return self._replies.get(prompt, "ok")


@pytest.fixture
def patched_build(monkeypatch):
    """Point runtime.build at a FakeRuntime the test controls."""
    holder = {}

    def _install(rt):
        holder["rt"] = rt
        monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: rt)
        return rt

    return _install


# ---------------------------------------------------------------------------
# cases.py — the format
# ---------------------------------------------------------------------------
def test_minimal_case_is_body_only(tmp_path):
    """The smallest possible case: no frontmatter, no headings."""
    hub = _hub(tmp_path, {"ping.md": "Reply with the word pong."})
    (case,) = cases.discover(hub)
    assert case.name == "ping"
    assert case.prompt == "Reply with the word pong."
    assert case.criteria is None
    assert case.is_judged is False
    assert case.timeout == cases.DEFAULT_TIMEOUT
    assert case.threshold == cases.DEFAULT_THRESHOLD


def test_criteria_section_turns_the_judge_on(tmp_path):
    """There is no `judge:` flag — writing criteria IS the switch."""
    hub = _hub(tmp_path, {"refund.md": (
        "## Prompt\nWhat is the refund window?\n\n"
        "## Criteria\nStates 14 days.\n"
    )})
    (case,) = cases.discover(hub)
    assert case.prompt == "What is the refund window?"
    assert case.criteria == "States 14 days."
    assert case.is_judged is True


def test_full_frontmatter_parses(tmp_path):
    hub = _hub(tmp_path, {"full.md": (
        "---\n"
        "schedule: \"0 6 * * 1\"\n"
        "tags: [canary, tier1]\n"
        "expect_tools: [read_knowledge]\n"
        "forbid_tools: [http_get]\n"
        "contains: [\"14 days\"]\n"
        "not_contains: [\"as an AI\"]\n"
        "timeout: 45\n"
        "threshold: 9\n"
        "enabled: true\n"
        "---\n"
        "## Prompt\nHello\n\n## Criteria\nBe nice.\n"
    )})
    (case,) = cases.discover(hub)
    assert case.tags == ["canary", "tier1"]
    assert case.expect_tools == ["read_knowledge"]
    assert case.forbid_tools == ["http_get"]
    assert case.contains == ["14 days"]
    assert case.not_contains == ["as an AI"]
    assert case.timeout == 45
    assert case.threshold == 9
    assert case.schedule == "0 6 * * 1"
    assert case.cron is not None
    assert case.is_scheduled is True


def test_bare_string_accepted_where_a_list_is_expected(tmp_path):
    """`contains: "14 days"` is what people write first. Accept it."""
    hub = _hub(tmp_path, {"s.md": "---\ncontains: \"14 days\"\n---\nHi"})
    (case,) = cases.discover(hub)
    assert case.contains == ["14 days"]


def test_unknown_frontmatter_key_is_an_error(tmp_path):
    """A typo'd key must not silently make a case assert nothing."""
    hub = _hub(tmp_path, {"typo.md": "---\nexpected_tools: [x]\n---\nHi"})
    with pytest.raises(cases.EvalCaseError, match="expected_tools"):
        cases.discover(hub)


def test_bad_cron_is_an_error(tmp_path):
    hub = _hub(tmp_path, {"c.md": "---\nschedule: \"not a cron\"\n---\nHi"})
    with pytest.raises(cases.EvalCaseError, match="schedule"):
        cases.discover(hub)


def test_empty_prompt_is_an_error(tmp_path):
    hub = _hub(tmp_path, {"e.md": "---\ntags: [x]\n---\n\n## Criteria\nnothing\n"})
    with pytest.raises(cases.EvalCaseError, match="no prompt"):
        cases.discover(hub)


def test_underscore_and_dot_files_are_ignored(tmp_path):
    hub = _hub(tmp_path, {"real.md": "Hi", "_draft.md": "Hi", ".hidden.md": "Hi"})
    assert [c.name for c in cases.discover(hub)] == ["real"]


def test_non_strict_discovery_skips_the_broken_file(tmp_path):
    """The scheduler must not be stopped by one unparseable case."""
    hub = _hub(tmp_path, {"good.md": "Hi", "bad.md": "---\nnope: 1\n---\nHi"})
    found = cases.discover(hub, strict=False)
    assert [c.name for c in found] == ["good"]


def test_no_evals_dir_is_empty_not_an_error(tmp_path):
    (tmp_path / "hub").mkdir()
    assert cases.discover(tmp_path / "hub") == []


def test_select_filters_by_tag_case_and_enabled(tmp_path):
    hub = _hub(tmp_path, {
        "a.md": "---\ntags: [canary]\n---\nHi",
        "b.md": "---\ntags: [slow]\n---\nHi",
        "c.md": "---\ntags: [canary]\nenabled: false\n---\nHi",
    })
    found = cases.discover(hub)
    assert [c.name for c in cases.select(found, tag="canary")] == ["a"]
    assert [c.name for c in cases.select(found, case="b*")] == ["b"]
    assert [c.name for c in cases.select(found, tag="canary",
                                         include_disabled=True)] == ["a", "c"]


# ---------------------------------------------------------------------------
# assertions.py — chrome stripping
# ---------------------------------------------------------------------------
def test_strip_chrome_removes_real_tool_event_output():
    """Fed with genuine tool_events output, so a format change breaks here
    rather than silently corrupting eval verdicts."""
    full = tool_events.format_call("read_knowledge", {"name": "policy"}, mode="full")
    compact = tool_events.format_call("http_get", {"url": "x"}, mode="compact")
    err = tool_events.format_error("odoo_info", "connection refused")
    text = f"The window is 14 days.{full}{compact}{err}"

    clean = assertions.strip_chrome(text)
    assert clean == "The window is 14 days."
    for noise in ("read_knowledge", "http_get", "odoo_info", "<details>", ">"):
        assert noise not in clean


def test_strip_chrome_removes_thinking_panel():
    text = "<think>\nlet me consider\n</think>\nThe answer is 14 days."
    assert assertions.strip_chrome(text) == "The answer is 14 days."


def test_strip_chrome_handles_unclosed_thinking():
    """A truncated stream must not leave the whole answer swallowed or the
    raw thinking exposed to assertions."""
    assert assertions.strip_chrome("answer<think>partial") == "answer"


def test_strip_chrome_keeps_ordinary_markdown():
    text = "# Heading\n\n- bullet\n\n**bold** and `code`"
    assert assertions.strip_chrome(text) == text


# ---------------------------------------------------------------------------
# assertions.py — the checks
# ---------------------------------------------------------------------------
def test_contains_is_case_insensitive():
    assert assertions.check_contains("The window is 14 DAYS.", ["14 days"])[0].passed


def test_contains_reports_the_missing_needle():
    (check,) = assertions.check_contains("nothing here", ["14 days"])
    assert not check.passed
    assert '"14 days"' in check.detail


def test_not_contains_catches_case_variants():
    (check,) = assertions.check_not_contains("An ERROR occurred", ["error"])
    assert not check.passed


def test_expect_tools_matches_through_the_mcp_prefix():
    """Backends prefix tool names; the hub author writes the short one."""
    checks = assertions.check_expect_tools(["mcp__hubzoid__read_knowledge"],
                                           ["read_knowledge"])
    assert checks[0].passed


def test_expect_tools_reports_a_tool_never_called():
    (check,) = assertions.check_expect_tools(["whoami"], ["read_knowledge"])
    assert not check.passed
    assert "never called" in check.detail


def test_forbid_tools_fails_when_the_tool_ran():
    (check,) = assertions.check_forbid_tools(["http_get"], ["http_get"])
    assert not check.passed
    assert "forbidden" in check.detail


def test_forbid_tools_passes_when_it_did_not():
    assert assertions.check_forbid_tools(["whoami"], ["http_get"])[0].passed


# ---------------------------------------------------------------------------
# runner.py
# ---------------------------------------------------------------------------
def test_runner_passes_a_green_case(tmp_path, patched_build):
    hub = _hub(tmp_path, {"ping.md": "---\ncontains: [pong]\n---\nsay pong"})
    patched_build(FakeRuntime(replies={"say pong": "pong!"}))

    suite = runner.run_suite(hub, cases.discover(hub))
    assert suite.ok and suite.passed == 1
    assert suite.cases[0].reason == ""


def test_runner_fails_and_explains(tmp_path, patched_build):
    hub = _hub(tmp_path, {"ping.md": "---\ncontains: [pong]\n---\nsay pong"})
    patched_build(FakeRuntime(replies={"say pong": "hello"}))

    suite = runner.run_suite(hub, cases.discover(hub))
    assert not suite.ok
    assert '"pong"' in suite.cases[0].reason


def test_runner_records_tool_calls_for_expect_tools(tmp_path, patched_build):
    hub = _hub(tmp_path, {"t.md": "---\nexpect_tools: [whoami]\n---\nwho am i"})
    patched_build(FakeRuntime(replies={"who am i": "you are shreya"},
                              tools={"who am i": ["whoami"]}))

    suite = runner.run_suite(hub, cases.discover(hub))
    assert suite.ok
    assert suite.cases[0].tool_calls == ["whoami"]


def test_tool_calls_are_recorded_even_with_tool_display_off(tmp_path, patched_build):
    """SHOW_TOOLS=off emits no text; expect_tools must still work. This is
    why the recorder sits at the call site, not in the rendered stream."""
    hub = _hub(tmp_path, {"t.md": "---\nexpect_tools: [whoami]\n---\nwho"})
    patched_build(FakeRuntime(replies={"who": "shreya"}, tools={"who": ["whoami"]}))
    suite = runner.run_suite(hub, cases.discover(hub))
    assert suite.ok
    assert "whoami" not in suite.cases[0].response


def test_runner_times_out_without_hanging(tmp_path, patched_build):
    hub = _hub(tmp_path, {"slow.md": "---\ntimeout: 1\n---\nslow"})
    patched_build(FakeRuntime(replies={"slow": "late"}, delay=5))

    suite = runner.run_suite(hub, cases.discover(hub))
    assert not suite.ok
    assert "timed out" in suite.cases[0].error


def test_runner_survives_a_crashing_case(tmp_path, patched_build):
    hub = _hub(tmp_path, {"boom.md": "boom"})
    patched_build(FakeRuntime(raises=RuntimeError("model down")))

    suite = runner.run_suite(hub, cases.discover(hub))
    assert not suite.ok
    assert "model down" in suite.cases[0].error


def test_agent_error_marker_is_reported_as_an_error(tmp_path, patched_build):
    """Backends yield '[agent error: ...]' instead of raising. A dead model
    must not read as 'failed on contains'."""
    hub = _hub(tmp_path, {"e.md": "---\ncontains: [pong]\n---\nhi"})
    patched_build(FakeRuntime(replies={"hi": "\n\n[agent error: AuthError: bad key]"}))

    suite = runner.run_suite(hub, cases.discover(hub))
    result = suite.cases[0]
    assert not result.passed
    assert "agent error" in result.error
    assert result.checks == []


def test_runtime_is_built_once_and_closed(tmp_path, patched_build):
    hub = _hub(tmp_path, {"a.md": "one", "b.md": "two"})
    rt = patched_build(FakeRuntime())

    runner.run_suite(hub, cases.discover(hub))
    assert rt.opened == 1 and rt.closed == 1
    assert rt.prompts == ["one", "two"]


def test_runtime_is_closed_even_when_a_case_crashes(tmp_path, patched_build):
    hub = _hub(tmp_path, {"a.md": "one"})
    rt = patched_build(FakeRuntime(raises=RuntimeError("nope")))
    runner.run_suite(hub, cases.discover(hub))
    assert rt.closed == 1


def test_judge_is_skipped_when_free_checks_fail(tmp_path, patched_build):
    """No point paying a model to grade an answer already known to be wrong."""
    hub = _hub(tmp_path, {"j.md": (
        "---\ncontains: [pong]\n---\n## Prompt\nsay pong\n\n## Criteria\nbe polite\n"
    )})
    patched_build(FakeRuntime(replies={"say pong": "hello"}))
    called = []

    async def judge(case, response, tool_calls=None, tools_available=None):
        called.append(case.name)
        return JudgeResult(score=10, threshold=7)

    suite = runner.run_suite(hub, cases.discover(hub), judge_fn=judge)
    assert called == []
    assert not suite.ok


def test_judge_runs_when_free_checks_pass(tmp_path, patched_build):
    hub = _hub(tmp_path, {"j.md": "## Prompt\nhi\n\n## Criteria\nbe polite\n"})
    patched_build(FakeRuntime(replies={"hi": "hello there"}))

    async def judge(case, response, tool_calls=None, tools_available=None):
        assert response == "hello there"
        return JudgeResult(score=9, threshold=7, model="judge-model")

    suite = runner.run_suite(hub, cases.discover(hub), judge_fn=judge)
    assert suite.ok
    assert suite.cases[0].judge.score == 9


def test_judge_below_threshold_fails_the_case(tmp_path, patched_build):
    hub = _hub(tmp_path, {"j.md": (
        "---\nthreshold: 8\n---\n## Prompt\nhi\n\n## Criteria\nbe polite\n"
    )})
    patched_build(FakeRuntime(replies={"hi": "meh"}))

    async def judge(case, response, tool_calls=None, tools_available=None):
        return JudgeResult(score=6, threshold=case.threshold)

    suite = runner.run_suite(hub, cases.discover(hub), judge_fn=judge)
    assert not suite.ok
    assert "6/10 < 8" in suite.cases[0].reason


def test_unjudged_case_ignores_the_judge(tmp_path, patched_build):
    """A case with no `## Criteria` never invokes the judge, even with one
    configured — that is the whole no-flag design."""
    hub = _hub(tmp_path, {"n.md": "---\ncontains: [hi]\n---\nsay hi"})
    patched_build(FakeRuntime(replies={"say hi": "hi"}))

    async def judge(case, response, tool_calls=None, tools_available=None):  # pragma: no cover — must not run
        raise AssertionError("judge ran on an unjudged case")

    assert runner.run_suite(hub, cases.discover(hub), judge_fn=judge).ok


# ---------------------------------------------------------------------------
# report.py — JSON, compare
# ---------------------------------------------------------------------------
def _suite(**verdicts) -> SuiteResult:
    """Build a SuiteResult from {case_name: passed_bool}."""
    from hubzoid.evals.assertions import Check

    suite = SuiteResult(hub="h")
    for name, ok in verdicts.items():
        suite.cases.append(CaseResult(
            name=name, checks=[Check("contains", ok, "" if ok else 'missing: "x"')]))
    return suite


def test_save_and_reload_round_trips(tmp_path, patched_build):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [ok]\n---\nhi"})
    patched_build(FakeRuntime(replies={"hi": "ok"}))
    suite = runner.run_suite(hub, cases.discover(hub))

    path = report.save(hub, suite)
    assert path.exists()
    back = report.latest(hub)
    assert back.passed == 1 and back.cases[0].name == "a"
    assert back.cases[0].response == "ok"


def test_json_is_valid_and_carries_the_schema(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    path = report.save(hub, _suite(a=True))
    data = json.loads(path.read_text())
    assert data["schema"] == 1 and data["passed"] == 1


def test_compare_reports_only_what_moved():
    prev = _suite(a=True, b=True, c=False)
    cur = _suite(a=True, b=False, c=True)
    deltas = {d.name: d.kind for d in report.compare(prev, cur)}
    assert deltas == {"b": "regression", "c": "fixed"}


def test_compare_is_empty_when_nothing_changed():
    assert report.compare(_suite(a=True), _suite(a=True)) == []


def test_compare_flags_added_and_removed():
    deltas = {d.name: d.kind for d in report.compare(_suite(a=True), _suite(b=True))}
    assert deltas == {"a": "removed", "b": "added"}


def test_compare_puts_regressions_first():
    prev = _suite(a=True, b=False)
    cur = _suite(a=False, b=True)
    assert report.compare(prev, cur)[0].kind == "regression"


def test_prune_keeps_the_cap(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setattr(report, "KEEP_RUNS", 3)
    for i in range(6):
        report.save(hub, _suite(a=True), stamp=f"2026010{i}_000000")
    assert len(list(report.runs_dir(hub).glob("*.json"))) == 3


def test_load_runs_skips_a_corrupt_file(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    report.save(hub, _suite(a=True), stamp="20260101_000000")
    (report.runs_dir(hub) / "20260102_000000.json").write_text("{not json")
    assert len(report.load_runs(hub, limit=5)) == 1


# ---------------------------------------------------------------------------
# _request_ctx — the recorder must be free when nothing is listening
# ---------------------------------------------------------------------------
def test_record_tool_call_is_a_noop_outside_a_recorder():
    _request_ctx.record_tool_call("whoami")     # must not raise or allocate


def test_recorder_collects_and_then_stops():
    with _request_ctx.tool_call_recorder() as calls:
        _request_ctx.record_tool_call("a")
        _request_ctx.record_tool_call("b")
    assert [c["name"] for c in calls] == ["a", "b"]
    _request_ctx.record_tool_call("c")
    assert len(calls) == 2
