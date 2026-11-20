"""A "Run for me" report started over the API (``workflows.api_runs``): as the
caller, through the chat tool's start path, with Idempotency-Key, refusals,
and the run in the person's history with ``trigger: "api"``. Real DBOS in a
subprocess (DBOS is a process-global singleton). Model-free."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tests.test_workflows_from_chat_engine import _SETUP, _WORKFLOW



_CONFIRM = textwrap.dedent('''
    from hubzoid import workflow

    @workflow(as_tool=True, confirm=True, run_as="reports@co.org")
    def careful_report():
        return {"answer": "done"}
''')

_RUN = textwrap.dedent('''
    import json, sys
    from dbos import DBOS
    from hubzoid.workflows import runtime, observe, api_runs
    hub = sys.argv[1]
    runtime.init(hub, hub_name="reports")
    runtime.load_workflows(hub)
    runtime.launch()
    from hubzoid.access import store_for
    store_for(hub).grant("meera@co.org", "reports", "wf_careful_report", actor="test")
    out = {}
    def refused(fn):
        try:
            fn()
            return None
        except api_runs.RunRefused as exc:
            return [exc.status, exc.code]
    who = dict(email="meera@co.org", account="id-meera@co.org")
    first = api_runs.start(hub, "program_report", inputs={"program": "Advanced", "period": "month"},
                           confirm=False, idempotency_key="nightly-2026-11-20", **who)
    again = api_runs.start(hub, "program_report", inputs={"program": "Advanced", "period": "month"},
                           confirm=False, idempotency_key="nightly-2026-11-20", **who)
    out["first"], out["again"] = first, again
    out["conflict"] = refused(lambda: api_runs.start(hub, "program_report", inputs={"program": "Intro"},
                              confirm=False, idempotency_key="nightly-2026-11-20", **who))
    out["bad_input"] = refused(lambda: api_runs.start(hub, "program_report", inputs={"period": "year"},
                               confirm=False, idempotency_key=None, **who))
    out["no_grant"] = refused(lambda: api_runs.start(hub, "program_report", inputs={}, confirm=False,
                              idempotency_key=None, email="ravi@co.org", account="id-ravi@co.org"))
    out["missing"] = refused(lambda: api_runs.start(hub, "nope", inputs={}, confirm=False,
                             idempotency_key=None, **who))
    out["needs_yes"] = refused(lambda: api_runs.start(hub, "careful_report", inputs={}, confirm=False,
                               idempotency_key=None, **who))
    out["bad_key"] = refused(lambda: api_runs.start(hub, "program_report", inputs={}, confirm=False,
                             idempotency_key="has space", **who))
    result = DBOS.retrieve_workflow(first["run_id"]).get_result()
    out["result_user"] = result["user"]
    out["mine"] = observe.personal_runs(hub, "meera@co.org", "id-meera@co.org", run_id=first["run_id"])
    out["ravi_sees"] = observe.personal_runs(hub, "ravi@co.org", "id-ravi@co.org", run_id=first["run_id"])
    print("RESULT " + json.dumps(out, default=str), flush=True)
    runtime.shutdown()
''')


@pytest.mark.slow
def test_a_run_for_me_report_over_the_api(tmp_path):
    hub = tmp_path / "reports"
    (hub / "workflows" / "program_report").mkdir(parents=True)
    (hub / "workflows" / "careful_report").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: reports\n---\nbody")
    (hub / "workflows" / "program_report" / "main.py").write_text(_WORKFLOW)
    (hub / "workflows" / "careful_report" / "main.py").write_text(_CONFIRM)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT", "DATABASE_URL",
                        "HUBZOID_SMTP_HOST", "HUBZOID_GATEWAY_ADMIN_EMAIL")}
    env.update(HUBZOID_OPERATIONAL_DB=f"sqlite:///{tmp_path / 'ops.db'}",
               HUBZOID_DBOS_DB=f"sqlite:///{tmp_path / 'dbos.db'}",
               HUBZOID_AUTH="true", HUBZOID_EMAIL_DELIVERY="preview", HUBZOID_SCHEDULES="1")
    subprocess.run([sys.executable, "-c", _SETUP, str(hub)], env=env, check=True,
                   capture_output=True, text=True)
    proc = subprocess.run([sys.executable, "-c", _RUN, str(hub)], env=env, capture_output=True,
                          text=True, timeout=240)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, proc.stderr[-4000:]
    out = json.loads(line[7:])
    assert out["first"]["created"] is True and out["first"]["run_id"].startswith("tool-program_report-")
    assert out["again"]["created"] is False and out["again"]["run_id"] == out["first"]["run_id"]
    assert out["conflict"] == [409, "idempotency_conflict"]
    assert out["bad_input"] == [422, "validation_failed"]
    assert out["no_grant"] == [403, "permission_denied"]
    assert out["missing"] == [404, "not_found"]
    assert out["needs_yes"] == [409, "confirm_required"]
    assert out["bad_key"] == [422, "validation_failed"]
    assert out["result_user"] == "meera@co.org"
    assert [r["id"] for r in out["mine"]] == [out["first"]["run_id"]]
    assert out["mine"][0]["links"] and out["ravi_sees"] == []


def test_the_route_runs_as_the_signed_in_person(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hubzoid.access import session
    from hubzoid.workflows import api_runs

    from fastapi import FastAPI
    from sqlalchemy import create_engine

    from hubzoid import access, db
    from hubzoid.portal import build_router

    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(tmp_path)
    gs.grant("meera@co.org", tmp_path.name.lower(), "use_hub", actor="test")
    monkeypatch.setattr(session, "verified_person", lambda *_a, **_k: ("id-meera", "meera@co.org"))
    seen = {}

    def fake_start(path, workflow, **kw):
        seen.update(kw, workflow=workflow)
        if workflow == "careful":
            raise api_runs.RunRefused(409, "confirm_required", "asks for a yes")
        return {"run_id": "tool-x-1", "status": "ENQUEUED", "created": kw["idempotency_key"] != "again"}

    monkeypatch.setattr(api_runs, "start", fake_start)
    app = FastAPI()
    app.include_router(build_router(tmp_path, admin_resolver=lambda r: None))
    c = TestClient(app)
    hub = tmp_path.name.lower()
    r = c.post("/portal/api/my/runs", json={"hub": hub, "workflow": "program_report", "inputs": {"program": "A"}},
               headers={"origin": "http://testserver", "idempotency-key": "k1"})
    assert r.status_code == 202, r.text
    assert r.json()["result"] == f"/portal/api/my/runs?hub={hub}&run_id=tool-x-1"
    assert seen["email"] == "meera@co.org" and seen["account"] == "id-meera" and seen["inputs"] == {"program": "A"}
    again = c.post("/portal/api/my/runs", json={"hub": hub, "workflow": "program_report"},
                   headers={"origin": "http://testserver", "idempotency-key": "again"})
    assert again.status_code == 200
    no = c.post("/portal/api/my/runs", json={"hub": hub, "workflow": "careful"}, headers={"origin": "http://testserver"})
    assert no.status_code == 409 and no.json()["code"] == "confirm_required"
    cross = c.post("/portal/api/my/runs", json={"hub": hub, "workflow": "program_report"},
                   headers={"origin": "https://evil.example"})
    assert cross.status_code == 403
    gs.revoke("meera@co.org", hub, "use_hub", actor="test")
    gone = c.post("/portal/api/my/runs", json={"hub": hub, "workflow": "program_report"},
                  headers={"origin": "http://testserver"})
    assert gone.status_code == 403


def test_api_runs_count_as_manual_starts_in_usage():
    from hubzoid import usage

    assert usage.run_trigger("api", "api") == "manual"
    assert usage.run_trigger("tool", "mcp") == "mcp"
