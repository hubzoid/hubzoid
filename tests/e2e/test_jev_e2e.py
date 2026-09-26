"""Jev with REAL calls: the adapter for each question type and a combined
request, then a workflow run through the CLI a builder uses.

    JEV_OPENROUTER_API_KEY=... pytest tests/e2e/test_jev_e2e.py -m e2e -v

Self-skips without JEV_OPENROUTER_API_KEY (OPENROUTER_API_KEY is never used for
Jev). About 12 decision requests, a fraction of a cent. The contract (types,
names, allowed values, ranges) is asserted; the judgments on these clear-cut
synthetic tickets are only reported as warnings when unexpected, since a
probability or a confidence is not proof of correctness.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import warnings

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("JEV_OPENROUTER_API_KEY", "").strip(),
                       reason="JEV_OPENROUTER_API_KEY not set"),
]

BILLING = "I was charged twice for my March invoice (INV-2291). Please refund the duplicate charge."
OUTAGE = ("Our whole team cannot log in since 9am. Every request returns HTTP 500 "
          "and production orders are blocked right now.")
IS_BILLING = {"is_billing": {"type": "noul", "instructions": "Does this support ticket describe a billing problem?",
                             "criteria": {"true": "Charges, invoices, refunds or payments.",
                                          "false": "Something other than billing."}}}
ROUTE = {"route": {"type": "choice", "instructions": "Which team should handle this ticket?",
                   "criteria": {"billing": "Charges, invoices, refunds, payment methods.",
                                "technical_support": "Errors, outages, bugs, performance.",
                                "account_support": "Login, profile, permissions, account settings."}}}
URGENCY = {"urgency": {"type": "score", "instructions": "How urgent is this ticket?",
                       "criteria": ["Low: no business impact, can wait a week",
                                    "Medium: some impact, handle within a day",
                                    "High: business blocked, handle within an hour"]}}


def _unit(x):
    return isinstance(x, (int, float)) and 0 <= x <= 1


def _check_contract(answers: dict, questions: dict):
    assert set(answers) == set(questions)
    for name, q in questions.items():
        a = answers[name]
        assert a["type"] == q["type"]
        if q["type"] == "noul":
            assert _unit(a["noul"])
        elif q["type"] == "choice":
            assert a["choice"] in q["criteria"]
            assert set(a.get("probabilities", {})) <= set(q["criteria"])
            assert all(map(_unit, a.get("probabilities", {}).values()))
            assert a.get("confidence") is None or _unit(a["confidence"])
        else:
            levels = len(q["criteria"])
            assert 0 <= a["score"] <= levels - 1
            assert set(a.get("probabilities", {})) <= {str(i) for i in range(levels)}
            assert a.get("confidence") is None or _unit(a["confidence"])


def _expect(label: str, ok: bool, got):
    if not ok:
        warnings.warn(f"unexpected Jev judgment for {label}: {got}", UserWarning, stacklevel=2)


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    d = tmp_path / "jev-live"
    d.mkdir()
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return d


@pytest.mark.parametrize("kind", ["noul", "choice", "score", "combined"])
def test_live_question_types(hub_dir, kind):
    from hubzoid import jev

    questions = {"noul": IS_BILLING, "choice": ROUTE, "score": URGENCY,
                 "combined": {**IS_BILLING, **ROUTE, **URGENCY}}[kind]
    billing = jev.call(hub_dir, {"state": BILLING, "questions": questions})
    outage = jev.call(hub_dir, {"state": {"subject": "Cannot log in", "body": OUTAGE},
                                "questions": questions})
    for reply in (billing, outage):
        assert reply["model"].startswith("typesafe/jev")
        assert reply["usage"]["input_tokens"] > 0
        _check_contract(reply["answers"], questions)
    b, o = billing["answers"], outage["answers"]
    if "is_billing" in questions:
        _expect("billing noul", b["is_billing"]["noul"] > 0.5, b["is_billing"])
        _expect("outage noul", o["is_billing"]["noul"] < 0.5, o["is_billing"])
    if "route" in questions:
        _expect("billing route", b["route"]["choice"] == "billing", b["route"])
        _expect("outage route", o["route"]["choice"] == "technical_support", o["route"])
    if "urgency" in questions:
        _expect("urgency order", o["urgency"]["score"] > b["urgency"]["score"],
                (b["urgency"]["score"], o["urgency"]["score"]))


_WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, step, workflow

    TICKET = %r
    QS = %r


    @step
    def choose(is_billing, route, urgency):
        if urgency["score"] >= 1.5:
            return "page_on_call"
        if route["choice"] == "billing" and is_billing["noul"] >= 0.5:
            return "billing_refund_queue"
        return route["choice"] + "_queue"


    @workflow()
    def jev_live():
        a = hub.call_jev(TICKET, {"is_billing": QS["is_billing"]})["is_billing"]
        b = hub.call_jev(TICKET, {"route": QS["route"]})["route"]
        c = hub.call_jev(TICKET, {"urgency": QS["urgency"]})["urgency"]
        both = hub.call_jev(TICKET, QS)
        return {"action": choose(a, b, c), "combined_route": both["route"]["choice"]}
''')


def test_live_workflow_through_the_cli(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL",
                        "HUBZOID_DEPLOYMENT", "OPENROUTER_API_KEY")}
    hub = tmp_path / "jev-hub"
    (hub / "workflows" / "jev_live").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: jev-hub\ndescription: Jev live check\n---\nTest hub.\n")
    (hub / ".env").write_text("MODEL=claude-local\n")
    (hub / "workflows" / "jev_live" / "main.py").write_text(
        _WORKFLOW % (OUTAGE, {**IS_BILLING, **ROUTE, **URGENCY}))
    hz = [sys.executable, "-m", "hubzoid"]
    run = subprocess.run(hz + ["schedule", "run", str(hub), "jev_live"], env=env,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    assert "returned" in run.stdout and "combined_route" in run.stdout
    _expect("workflow branch", "page_on_call" in run.stdout, run.stdout[-300:])
    key = env["JEV_OPENROUTER_API_KEY"].strip()
    assert key not in run.stdout and key not in run.stderr
    status = subprocess.run(hz + ["schedule", "status", str(hub)], env=env,
                            capture_output=True, text=True, timeout=120)
    assert "jev_live · SUCCESS" in status.stdout, status.stdout[-2000:]

    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    with eng.connect() as c:
        rows = c.execute(text("SELECT kind, subject, model, input_tokens, cost_usd, status "
                              "FROM hz_usage WHERE kind = 'jev'")).fetchall()
    assert len(rows) == 4
    assert all(r.subject == "workflow:jev_live" and r.status == "ok" and r.model.startswith("typesafe/jev")
               and r.input_tokens > 0 and r.cost_usd for r in rows)
