"""Workflows from chat through real DBOS runs: a team run and a person's run
of one workflow, inputs, the run's envelope in the history, a personal
schedule's first run, and a run refused because the person lost the report.
Runs in a subprocess because DBOS is a process-global singleton. Model-free."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.slow

_WORKFLOW = textwrap.dedent('''
    from typing import Literal
    from pydantic import BaseModel
    from hubzoid import workflow, hub

    class Inputs(BaseModel):
        program: str = "All"
        period: Literal["week", "month"] = "week"

    @workflow(schedule="daily at 06:00", run_as="reports@co.org", inputs=Inputs,
              schedule_input={"program": "Bhava"}, as_tool=True, schedulable=True)
    def program_report():
        page = hub.run_dir / "page.html"
        page.write_text(f"<h1>{hub.input.program} {hub.input.period}</h1>")
        art = hub.publish_artifact(page, title=f"{hub.input.program} report",
                                   key=f"program-{hub.input.program.lower()}")
        return {"team": hub.team_run, "trigger": hub.trigger, "user": hub.user.id,
                "started_by": hub.started_by, "url": art["latest_url"]}
''')

_SETUP = textwrap.dedent('''
    import sys
    from hubzoid.access import store_for
    gs = store_for(sys.argv[1])
    for who in ("reports@co.org", "meera@co.org", "ravi@co.org"):
        gs.upsert_identity(email=who, owui_id="id-" + who)
        gs.grant(who, "irs", "use_hub", actor="test")
    for perm in ("wf_program_report", "schedule_reports"):
        gs.grant("meera@co.org", "irs", perm, actor="test")
''')

_RUN = textwrap.dedent('''
    import json, sys, time
    from dbos import DBOS
    from hubzoid.workflows import runtime, personal, observe
    hub = sys.argv[1]
    runtime.init(hub, hub_name="irs")
    runtime.load_workflows(hub)
    runtime.launch()
    def done(handle):
        try:
            return handle.get_result()
        except Exception as exc:
            return {"error": type(exc).__name__, "message": str(exc)}
    out = {"mine_id": personal.tool_run_prefix("program_report", "meera@co.org", "id-meera@co.org") + "inputs-test-1"}
    out["team"] = done(runtime.start("program_report", scheduled_at="2026-10-12T00:30:00+00:00"))
    out["mine"] = done(runtime.start_run("program_report", {
        "trigger": "tool", "scope": "personal", "acts_as": "meera@co.org", "started_by": "meera@co.org",
        "acts_as_account": "id-meera@co.org",
        "inputs": {"program": "Samyama", "period": "month"}, "inputs_display": "Samyama, month"},
        workflow_id=out["mine_id"]))
    out["refused"] = done(runtime.start_run("program_report", {
        "trigger": "tool", "scope": "personal", "acts_as": "ravi@co.org", "started_by": "ravi@co.org",
        "inputs": {}}, workflow_id="tool-test-2"))
    proposal = personal.propose_create(hub, "irs", "meera@co.org", workflow="program_report",
        inputs={"program": "Bhava", "period": "month"}, cadence="every monday at 09:00",
        timezone_name="Asia/Kolkata")
    saved = personal.confirm(hub, "irs", "meera@co.org", proposal["code"])
    done(DBOS.retrieve_workflow(saved["run_id"]))
    s = personal.get(hub, saved["schedule"].id)
    out["schedule"] = {"state": s.state, "last": s.last_status}
    rows = {r["id"]: r for r in observe.runs(hub, limit=10, trusted=True)}
    out["history"] = {k: {"trigger": rows[k]["trigger"], "started_by": rows[k]["started_by"],
                          "inputs": rows[k]["inputs"], "run_as": rows[k]["run_as"]}
                      for k in (out["mine_id"],) if k in rows}
    out["personal"] = observe.personal_runs(hub, "meera@co.org", "id-meera@co.org")
    # Persist the exact handoff state a crash before queue admission leaves.
    out["recover"] = {"schedule": s.id, "run": "ps-" + s.id + "-restart"}
    personal._update(hub, s.id, last_run=out["recover"]["run"], last_status="dispatching")
    print("RESULT " + json.dumps(out), flush=True)
    runtime.shutdown()
''')

_RECOVER = textwrap.dedent('''
    import json, sys
    from dbos import DBOS
    from hubzoid.workflows import runtime, personal, observe
    hub, sid, run_id = sys.argv[1:]
    runtime.init(hub, hub_name="irs")
    runtime.load_workflows(hub)
    runtime.launch()
    started = personal.fire_due(hub, "irs")
    result = DBOS.retrieve_workflow(run_id).get_result()
    # The same durable admission id is idempotent in the real engine too.
    schedule = personal.get(hub, sid)
    second = runtime.start_run(schedule.workflow,
        personal.envelope(schedule, runtime._REGISTRY[schedule.workflow]),
        workflow_id=run_id).get_result()
    rows = [r for r in observe.schedule_runs(hub, sid) if r["id"] == run_id]
    print("RESULT " + json.dumps({"started": started,
          "same": result == second, "count": len(rows), "status": schedule.last_status}), flush=True)
    runtime.shutdown()
''')


def test_team_and_personal_runs_of_one_workflow(tmp_path):
    hub = tmp_path / "irs"
    (hub / "workflows" / "program_report").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: irs\n---\nbody")
    (hub / "workflows" / "program_report" / "main.py").write_text(_WORKFLOW)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT", "DATABASE_URL",
                        "HUBZOID_SMTP_HOST", "HUBZOID_GATEWAY_ADMIN_EMAIL")}
    env.update(HUBZOID_OPERATIONAL_DB=f"sqlite:///{tmp_path / 'ops.db'}",
               HUBZOID_DBOS_DB=f"sqlite:///{tmp_path / 'dbos.db'}",
               HUBZOID_AUTH="true", HUBZOID_EMAIL_DELIVERY="preview")
    subprocess.run([sys.executable, "-c", _SETUP, str(hub)], env=env, check=True,
                   capture_output=True, text=True)
    proc = subprocess.run([sys.executable, "-c", _RUN, str(hub)], env=env, capture_output=True,
                          text=True, timeout=240)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, proc.stderr[-4000:]
    out = json.loads(line[7:])
    assert out["team"]["team"] is True and out["team"]["user"] == "reports@co.org"
    assert out["team"]["url"].endswith("/portal/latest/irs/program-bhava")       # schedule_input
    assert out["mine"]["team"] is False and out["mine"]["user"] == "meera@co.org"
    assert "/portal/latest/irs/program-samyama/by/" in out["mine"]["url"]
    assert out["refused"]["error"] == "IdentityError"                            # no report grant
    assert out["schedule"] == {"state": "active", "last": "updated"}
    assert out["history"][out["mine_id"]] == {"trigger": "tool", "started_by": "meera@co.org",
                                             "inputs": "Samyama, month", "run_as": "meera@co.org"}
    assert [r["id"] for r in out["personal"]] == [out["mine_id"]]
    assert out["personal"][0]["links"][0]["url"] == out["mine"]["url"]
    recovered = subprocess.run([sys.executable, "-c", _RECOVER, str(hub),
                                out["recover"]["schedule"], out["recover"]["run"]],
                               env=env, capture_output=True, text=True, timeout=120)
    line = next((l for l in recovered.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, recovered.stderr[-4000:]
    assert json.loads(line[7:]) == {"started": [out["recover"]["run"]],
                                   "same": True, "count": 1, "status": "no_change"}
