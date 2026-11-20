"""`hubzoid eval run --required`: the CI release gate runs exactly what
quality/manifest.yaml requires, and refuses (exit 2) whenever the gate could
check less than the manifest asks. Also the provenance every results file now
records (Hubzoid version, the hub's Git commit)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import __version__, cli
from hubzoid.evals import junit, quality
from hubzoid.evals.results import SuiteResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_langfuse(monkeypatch):
    monkeypatch.delenv("HUBZOID_OTEL_ENDPOINT", raising=False)


@pytest.fixture
def fake_model(monkeypatch):
    """A runtime that replies "ok"; records which prompts ran."""
    seen: list[str] = []

    class FakeRuntime:
        name = "fake-model"

        async def aopen(self): ...
        async def aclose(self): ...

        async def run(self, prompt):
            seen.append(prompt)
            return "ok"

    monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: FakeRuntime())
    return seen


def _hub(tmp_path: Path, cases: dict[str, str], manifest: str | None) -> Path:
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: H\ndescription: D\n---\nBe good.\n")
    for name, text in cases.items():
        (hub / "evals" / name).write_text(text, encoding="utf-8")
    if manifest is not None:
        (hub / "quality").mkdir()
        (hub / "quality" / "manifest.yaml").write_text(manifest)
    return hub


GOOD = "---\ncontains: ok\n---\nSay ok\n"
BAD = "---\ncontains: never\n---\nSay ok\n"
EXTRA = "---\ncontains: ok\n---\nNot required\n"


def _run(*args):
    return runner.invoke(cli.app, ["eval", "run", *args])


def test_runs_exactly_the_required_cases(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": GOOD, "b.md": "---\ncontains: ok\ntags: [safety]\n---\nTagged\n",
                          "extra.md": EXTRA},
               "version: 1\nrequired: [a]\nrequired_tags: [safety]\n")
    r = _run(str(hub), "--required")
    assert r.exit_code == 0, r.output
    assert sorted(fake_model) == ["Say ok", "Tagged"]


def test_a_failing_required_case_exits_one(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": BAD}, "version: 1\nrequired: [a]\n")
    assert _run(str(hub), "--required").exit_code == 1


def test_doctor_agrees_after_a_passing_required_run(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": GOOD}, "version: 1\nrequired: [a]\n")
    assert _run(str(hub), "--required").exit_code == 0
    assert [e.status for e in quality.evidence(hub)] == ["ok"]


@pytest.mark.parametrize("cases,manifest,needle", [
    ({"a.md": GOOD}, None, "needs quality/manifest.yaml"),
    ({"a.md": GOOD}, "version: 1\n", "requires no cases"),
    ({"a.md": GOOD}, "version: 1\nrequired: [typo]\n", "no eval case matches"),
    ({"a.md": GOOD}, "version: 1\nrequired_tags: [missing]\n", "no eval case has the tag"),
    ({"a.md": GOOD}, "version: 2\nrequired: [a]\n", "version: 1"),
    ({"a.md": "---\ncontains: ok\nenabled: false\n---\nOff\n"}, "version: 1\nrequired: [a]\n", "switched off"),
    ({"a.md": "Just a prompt\n"}, "version: 1\nrequired: [a]\n", "check nothing"),
    ({"a.md": "---\nbogus_key: 1\n---\nBroken\n"}, "version: 1\nrequired: [a]\n", "bogus_key"),
])
def test_an_untrustworthy_gate_exits_two_before_any_model_call(tmp_path, fake_model, cases, manifest, needle):
    hub = _hub(tmp_path, cases, manifest)
    r = _run(str(hub), "--required")
    assert r.exit_code == 2, r.output
    assert needle in r.output
    assert fake_model == []


@pytest.mark.parametrize("flag", [["--tag", "x"], ["--case", "a"], ["--no-judge"], ["--model", "haiku"]])
def test_filters_that_would_check_less_are_refused(tmp_path, fake_model, flag):
    hub = _hub(tmp_path, {"a.md": GOOD}, "version: 1\nrequired: [a]\n")
    r = _run(str(hub), "--required", *flag)
    assert r.exit_code == 2
    assert fake_model == []


def test_json_error_names_the_problem(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": GOOD}, None)
    r = _run(str(hub), "--required", "--json")
    assert r.exit_code == 2
    assert json.loads(r.output)["detail"]["code"] == "invalid_manifest"


def test_a_judged_or_faked_case_counts_as_a_check(tmp_path):
    hub = _hub(tmp_path, {
        "judged.md": "## Prompt\nAnswer well\n\n## Criteria\nIt is polite.\n",
        "faked.md": "---\nfake_tools:\n  - {tool: lookup, output: x}\n---\nLook it up\n",
    }, "version: 1\nrequired: ['*']\n")
    assert [c.name for c in quality.gate_cases(hub)] == ["faked", "judged"]


def test_results_record_version_and_commit(tmp_path, fake_model):
    hub = _hub(tmp_path, {"a.md": GOOD}, "version: 1\nrequired: [a]\n")
    subprocess.run(["git", "init", "-q", str(hub)], check=True)
    subprocess.run(["git", "-C", str(hub), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    r = _run(str(hub), "--required", "--json", "--junit", str(tmp_path / "j.xml"))
    assert r.exit_code == 0, r.output
    record = json.loads(r.output)
    head = subprocess.run(["git", "-C", str(hub), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert record["hubzoid_version"] == __version__
    assert record["git_commit"] == head
    assert record["git_dirty"] is True          # the hub's files were never committed
    subprocess.run(["git", "-C", str(hub), "add", "-A", "--", ".", ":(exclude).hubzoid"], check=True)
    subprocess.run(["git", "-C", str(hub), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-q", "-m", "hub"], check=True)
    assert quality.provenance(hub)[2] is False  # its own results folder does not count
    xml = (tmp_path / "j.xml").read_text()
    assert f'name="git_commit" value="{head}"' in xml
    assert 'name="git_dirty" value="true"' in xml


def test_provenance_outside_git_leaves_commit_empty(tmp_path):
    version, commit, dirty = quality.provenance(tmp_path)
    assert version == __version__ and commit is None and dirty is None


def test_old_results_files_still_load():
    suite = SuiteResult.from_dict({"schema": 2, "hub": "h", "cases": []})
    assert suite.hubzoid_version is None and suite.git_commit is None and suite.git_dirty is None
    assert b"hubzoid_version" in junit.to_xml(suite)
