"""Tests for `hubzoid eval run / list / status / explain`.

The load-bearing one is the exit code: it is what makes the same binary a CI
gate, so a failing case must exit non-zero and a passing suite must exit 0.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch):
    """Keep the CLI off the network regardless of the developer's own env."""
    monkeypatch.delenv("HUBZOID_OTEL_ENDPOINT", raising=False)


def _hub(tmp_path: Path, files: dict[str, str]) -> Path:
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: H\ndescription: D\n---\nBe good.\n")
    for name, text in files.items():
        (hub / "evals" / name).write_text(text, encoding="utf-8")
    return hub


@pytest.fixture
def fake_model(monkeypatch):
    """Install a runtime that replies with whatever the test asks for."""
    def _install(reply="ok", tools=()):
        class FakeRuntime:
            name = "fake-model"

            async def aopen(self): ...
            async def aclose(self): ...

            async def run(self, prompt):
                from hubzoid import _request_ctx
                for t in tools:
                    _request_ctx.record_tool_call(t)
                return reply

        monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: FakeRuntime())
    return _install


def _run(*args):
    return runner.invoke(cli.app, ["eval", *args])


# ---------------------------------------------------------------------------
# run — the CI gate
# ---------------------------------------------------------------------------
def test_passing_suite_exits_zero(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nsay pong"})
    fake_model("pong")
    result = _run("run", str(hub))
    assert result.exit_code == 0
    assert "1 passed" in result.stdout


def test_failing_suite_exits_non_zero(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nsay pong"})
    fake_model("hello")
    result = _run("run", str(hub))
    assert result.exit_code == 1
    assert "1 failed" in result.stdout


def test_a_broken_case_file_is_a_clean_error_not_a_traceback(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\nexpected_tools: [x]\n---\nhi"})
    result = _run("run", str(hub))
    assert result.exit_code == 2
    assert "expected_tools" in result.stdout


def test_no_cases_is_a_friendly_zero(tmp_path):
    hub = _hub(tmp_path, {})
    result = _run("run", str(hub))
    assert result.exit_code == 0
    assert "no eval cases" in result.stdout


def test_tag_filter_selects_a_subset(tmp_path, fake_model):
    hub = _hub(tmp_path, {
        "a.md": "---\ntags: [canary]\ncontains: [ok]\n---\nhi",
        "b.md": "---\ntags: [slow]\ncontains: [nope]\n---\nhi",
    })
    fake_model("ok")
    result = _run("run", str(hub), "--tag", "canary")
    assert result.exit_code == 0
    assert "1 case(s)" in result.stdout


def test_case_glob_selects_a_subset(tmp_path, fake_model):
    hub = _hub(tmp_path, {"refund-a.md": "---\ncontains: [ok]\n---\nhi",
                          "other.md": "---\ncontains: [nope]\n---\nhi"})
    fake_model("ok")
    assert _run("run", str(hub), "--case", "refund-*").exit_code == 0


def test_a_filter_matching_nothing_is_not_an_error(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "hi"})
    result = _run("run", str(hub), "--tag", "nonexistent")
    assert result.exit_code == 0
    assert "no cases matched" in result.stdout


def test_no_judge_skips_the_model_tier(tmp_path, fake_model, monkeypatch):
    hub = _hub(tmp_path, {"a.md": "## Prompt\nhi\n\n## Criteria\nbe nice\n"})
    fake_model("hello")

    def boom(*a, **k):  # pragma: no cover — must not run
        raise AssertionError("judge was built despite --no-judge")

    monkeypatch.setattr("hubzoid.evals.judge.make_judge", boom)
    result = _run("run", str(hub), "--no-judge")
    assert result.exit_code == 0
    assert "judge: off" in result.stdout


def test_judge_runs_and_can_fail_the_case(tmp_path, fake_model, monkeypatch):
    hub = _hub(tmp_path, {"a.md": "## Prompt\nhi\n\n## Criteria\nbe nice\n"})
    fake_model("hello")

    async def ask(model_id, prompt):
        return '{"score": 3, "reasoning": "curt"}'

    monkeypatch.setattr("hubzoid.evals.judge.ask_model", ask)
    result = _run("run", str(hub), "--judge-model", "fake-judge")
    assert result.exit_code == 1
    assert "3/10" in result.stdout


def test_run_writes_the_json_record(tmp_path, fake_model):
    from hubzoid.evals import report as report_lib

    hub = _hub(tmp_path, {"a.md": "---\ncontains: [ok]\n---\nhi"})
    fake_model("ok")
    _run("run", str(hub))
    assert report_lib.latest(hub).passed == 1


def test_compare_reports_a_regression(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nsay pong"})
    fake_model("pong")
    assert _run("run", str(hub)).exit_code == 0

    fake_model("hello")
    result = _run("run", str(hub), "--compare")
    assert result.exit_code == 1
    assert "REGRESSIONS" in result.stdout
    assert "PASS" in result.stdout and "FAIL" in result.stdout


def test_compare_on_the_first_ever_run_is_not_an_error(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [ok]\n---\nhi"})
    fake_model("ok")
    result = _run("run", str(hub), "--compare")
    assert result.exit_code == 0
    assert "no previous run" in result.stdout


# ---------------------------------------------------------------------------
# list / status / explain
# ---------------------------------------------------------------------------
def test_list_shows_what_each_case_checks(tmp_path):
    hub = _hub(tmp_path, {
        "a.md": '---\nschedule: "0 6 * * 1"\nexpect_tools: [whoami]\n---\nhi',
        "b.md": "## Prompt\nhi\n\n## Criteria\nbe nice\n",
        "c.md": "---\nenabled: false\n---\nhi",
    })
    out = _run("list", str(hub)).stdout
    assert "expects whoami" in out
    assert "judged" in out and "free only" in out
    assert "Mon at 06:00" in out
    assert "disabled" in out


def test_list_on_an_empty_hub(tmp_path):
    assert "no eval cases" in _run("list", str(_hub(tmp_path, {}))).stdout


def test_status_before_any_run(tmp_path):
    assert "no eval runs yet" in _run("status", str(_hub(tmp_path, {}))).stdout


def test_status_reports_the_last_run_and_failures(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\n---\nhi"})
    fake_model("nope")
    _run("run", str(hub))
    out = _run("status", str(hub)).stdout
    assert "1 failing" in out and "a" in out


def test_explain_gathers_everything_needed_to_fix_a_case(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "---\ncontains: [pong]\nexpect_tools: [whoami]\n---\nsay pong"})
    fake_model("hello there", tools=["whoami"])
    _run("run", str(hub))

    out = _run("explain", str(hub), "a").stdout
    assert "say pong" in out           # the prompt
    assert "hello there" in out        # the response
    assert "whoami" in out             # the tools it called
    assert "AGENTS.md" in out          # the spec to edit
    assert "a.md" in out               # the case file to edit


def test_explain_on_an_unknown_case_lists_what_ran(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": "hi"})
    fake_model("ok")
    _run("run", str(hub))
    result = _run("explain", str(hub), "nope")
    assert result.exit_code == 2
    assert "Ran: a" in result.stdout
