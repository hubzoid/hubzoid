"""The Watchtower sample (`hubzoid init <name> --template watchtower`).

Acceptance without a model: detection is plain code, and the model call is
replaced by a fixed Finding through the same `call_llm` seam, so the checks
run anywhere. tests/e2e/test_watchtower_e2e.py runs it with a real model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from hubzoid import cli

_RUN = textwrap.dedent('''
    import json, sys
    from hubzoid.workflows import context, runtime

    calls = []

    def fake_llm(spec, hub_dir=None, subject=None):
        calls.append(spec)
        data = {"severity": "critical", "summary": "checkout is slow and failing",
                "likely_cause": "the 09:45 deploy", "next_step": "check the payment client"}
        return {"text": json.dumps(data), "json": data, "model": "fake"}

    context.configure(llm=fake_llm)
    runtime.init(sys.argv[1])
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    try:
        out = runtime.run_now("watchtower")
        print("RESULT " + json.dumps({"out": out, "calls": len(calls),
                                      "prompt": calls[0]["prompt"] if calls else ""}))
    except Exception as exc:
        print("FAILED " + str(exc))
    runtime.shutdown()
''')


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["init", "wt", "--template", "watchtower"])
    assert r.exit_code == 0, r.output
    hub = tmp_path / "wt"
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT")}
    return hub, env


def _run(hub, env):
    proc = subprocess.run([sys.executable, "-c", _RUN, str(hub)], capture_output=True,
                          text=True, timeout=180, env=env)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(("RESULT ", "FAILED "))), None)
    assert line, proc.stderr[-2000:]
    return (json.loads(line[7:]), None) if line.startswith("RESULT ") else (None, line[7:])


def test_init_gives_a_clean_sample(sample):
    hub, _ = sample
    for rel in ("AGENTS.md", "README.md", "workflows/watchtower/main.py", "workflows/settings.yaml",
                "raw_data/events/metrics.jsonl", "raw_data/samples/broken.jsonl", "knowledge/watchtower.md"):
        assert (hub / rel).is_file(), rel
    assert not list(hub.rglob("__pycache__")) and not (hub / ".hubzoid").exists()
    assert "synthetic" in (hub / "raw_data" / "README.md").read_text().lower()


def test_a_run_detects_explains_and_remembers(sample):
    hub, env = sample
    result, err = _run(hub, env)
    assert err is None, err
    assert result["out"]["breaches"] == 1 and result["out"]["new"] == 1 and result["calls"] == 1
    assert '"service": "checkout"' in result["prompt"] and "deploy" in result["prompt"]
    report = (hub / "output" / "watchtower" / "latest.md").read_text()
    assert "## checkout: critical" in report and "Peak error rate" in report
    assert "search" not in report and "auth" not in report  # healthy services stay quiet

    again, _ = _run(hub, env)  # already explained: no model call, no new report
    assert again["out"]["new"] == 0 and again["calls"] == 0


def test_thresholds_come_from_settings(sample):
    hub, env = sample
    (hub / "workflows" / "settings.yaml").write_text("watchtower:\n  p95_ms: 5000\n  error_rate: 0.5\n")
    result, _ = _run(hub, env)
    assert result["out"] == {"breaches": 0, "new": 0, "report": None}


def test_the_controlled_failure_names_the_bad_line(sample):
    hub, env = sample
    (hub / "raw_data" / "events" / "broken.jsonl").write_text(
        (hub / "raw_data" / "samples" / "broken.jsonl").read_text())
    _, err = _run(hub, env)
    assert err and "broken.jsonl line 2 is not valid JSON" in err
