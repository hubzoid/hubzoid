"""Model-free eval selection, recording, argument safety and structured validation."""
from __future__ import annotations

import json
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hubzoid import structured
from hubzoid.evals import calls, cases, judge, langfuse, report, runner, suite
from hubzoid.evals.results import CaseResult, SuiteResult, ToolCallRecord
from tests.test_evals import _hub


@pytest.mark.parametrize(("names", "message"), [
    (None, "no enabled eval cases"), ([], "no eval cases named"),
    (["unknown"], "unknown eval case.*Known: disabled"),
])
def test_selection_refuses_empty_and_unknown_cases(tmp_path, names, message):
    hub = _hub(tmp_path, {"disabled.md": "---\nenabled: false\n---\nhello"})
    with pytest.raises(cases.EvalCaseError, match=message):
        suite.select_cases(hub, names)


def test_explicit_selection_deduplicates_and_can_include_disabled_cases(tmp_path):
    hub = _hub(tmp_path, {"a.md": "hello", "b.md": "---\nenabled: false\n---\nother"})
    assert [c.name for c in suite.select_cases(hub)] == ["a"]
    assert [c.name for c in suite.select_cases(hub, ["b", "a", "b"])] == ["b", "a"]


def test_suite_account_and_trigger_validation():
    assert suite.check_run_as(None) is None
    assert suite.check_run_as("  ") is None
    assert suite.check_run_as(" Person@Example.org ") == "person@example.org"
    with pytest.raises(cases.EvalCaseError, match="email"):
        suite.check_run_as("not-an-email")
    with pytest.raises(ValueError, match="trigger must be one of"):
        suite._check_trigger("unknown")


@pytest.mark.asyncio
async def test_async_suite_records_judge_model_and_saved_results(tmp_path, monkeypatch):
    hub = _hub(tmp_path, {"a.md": "## Prompt\nhello\n## Criteria\nbe helpful"})
    judge_fn = SimpleNamespace(model_id="fake-grader")
    monkeypatch.setattr(judge, "make_judge", lambda *a, **kw: judge_fn)
    fake_run = AsyncMock(return_value=SuiteResult(hub="hub", cases=[CaseResult(name="a", response="hello")]))
    monkeypatch.setattr(runner, "arun_suite", fake_run)
    push = Mock(return_value="trace-id")
    monkeypatch.setattr(langfuse, "push", push)
    path, saved = await suite.arun_and_save(hub, trigger="ci", judge_model="fake-grader")
    loaded = report.latest(hub)
    assert path.is_file()
    assert loaded.judge_model == "fake-grader" and loaded.cases[0].response == "hello"
    assert saved.judge_model == "fake-grader"
    assert fake_run.call_args.kwargs["judge_fn"] is judge_fn
    assert fake_run.call_args.kwargs["trigger"] == "ci"
    push.assert_called_once_with(hub.resolve(), saved)


def test_sync_suite_preserves_results_when_telemetry_fails(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path, {"a.md": "hello"})
    monkeypatch.setattr(runner, "arun_suite", AsyncMock(return_value=SuiteResult(hub="hub", cases=[
        CaseResult(name="a", response="stored answer")])))
    monkeypatch.setattr(langfuse, "push", Mock(side_effect=OSError("telemetry down")))
    path, saved = suite.run_and_save(hub, judge=False, trigger="ci")
    assert path.is_file() and report.latest(hub).cases[0].response == "stored answer"
    assert saved.cases[0].passed
    assert "langfuse push skipped: telemetry down" in caplog.text


def test_safe_arguments_are_bounded_and_redacted_before_storage():
    assert calls.normalize_args(" ") == {}
    assert calls.normalize_args("bad json") == "bad json"
    assert calls.safe_args(None) is None
    assert calls.safe_args("[1,2]") == {"input": [1, 2]}
    compact = calls.safe_args({"api_key": "secret", "query": "x" * 501, "nested": ["short"]})
    assert compact["api_key"] == "[redacted]" and compact["query"] == "x" * 500 + "…"
    huge = calls.safe_args({f"field{n}": "x" * 500 for n in range(30)})
    assert set(huge) == {"preview"} and len(huge["preview"]) == calls.ARGS_MAX_JSON - 47
    assert huge["preview"].endswith("…")
    assert calls.preview(None) is None and calls.preview(" \n ") is None
    assert calls.preview("x" * 600) == "x" * 499 + "…"
    assert calls.error_text("x\n   y") == "x y"
    assert calls.error_text("x" * 500) == "x" * 299 + "…"
    assert calls.error_text(None) is None


def test_argument_matching_and_human_descriptions():
    assert calls.args_match('{"q":"Quarterly Revenue","limit":5}', {"q": "revenue", "limit": 5})
    assert calls.args_match("not json", {"q": "x"}) is False
    assert calls.value_matches(None, "value") is False
    assert calls.value_matches({"nested": "yes"}, "YES") is True
    assert calls.key_args({"x": "two words", "y": 3}, limit=1) == 'x="two words" …'
    record = ToolCallRecord(name="lookup", args={"key": "item"}, ok=False, error="denied", duration_ms=12, turn=2)
    assert calls.describe(record, turn=True) == "turn 2  lookup key=item  failed: denied  12 ms"
    assert calls.describe(ToolCallRecord(name="lookup", ok=True)) == "lookup  ok"
    assert calls.describe(ToolCallRecord(name="lookup")) == "lookup"
    assert calls.expected_text({"name": "partial", "limit": 3}) == 'name containing "partial", limit=3'


@pytest.mark.parametrize(("value", "schema", "errors"), [
    (False, {"type": "integer"}, ["/: expected integer"]),
    (None, {"type": ["null", "string"]}, []),
    ("a", {"minLength": 2, "enum": ["allowed"]}, ["/: not one of the allowed values", "/: shorter than 2 characters"]),
    ("long", {"maxLength": 2}, ["/: longer than 2 characters"]),
    (1, {"minimum": 2}, ["/: below 2"]),
    (5, {"maximum": 4}, ["/: above 4"]),
    ([1], {"minItems": 2}, ["/: fewer than 2 items"]),
    ([1, 2], {"maxItems": 1}, ["/: more than 1 items"]),
    ({"extra": 1, "count": "no"}, {"required": ["name"], "properties": {"count": {"type": "integer"}},
        "additionalProperties": False}, ["/name: missing", "/extra: not allowed", "/count: expected integer"]),
    (["x"], {"items": {"type": "number"}}, ["/0: expected number"]),
    ({}, None, []),
])
def test_structured_schema_reports_precise_paths(value, schema, errors):
    assert structured.schema_errors(value, schema) == errors


def test_structured_error_pickles_raw_reply_and_parser_finds_arrays():
    err = structured.ModelOutputError("invalid output", "raw reply")
    restored = pickle.loads(pickle.dumps(err))
    assert str(restored) == "invalid output" and restored.raw == "raw reply"
    assert structured.extract_json("Result: [1, 2] done") == [1, 2]
    assert "matches this JSON Schema" in structured.json_instruction({"type": "object"})
