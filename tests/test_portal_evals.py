"""Evals in the Console (hubzoid/portal_evals.py): read a hub's cases and
results, and start a run as a DBOS workflow on the hub's own engine.

No model is ever called: `hubzoid.evals.run_and_save` is stubbed with the
contract's signature wherever a run actually executes. DBOS is a process-global
singleton, so a run that executes does so in a subprocess."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import portal_evals
from hubzoid.portal import PortalAdmin, build_router

ROOT = Path(__file__).resolve().parents[1]

CASE_REFUND = """---
tags: [canary]
schedule: "0 6 * * 1"
expect_tools: [read_knowledge]
---
## Prompt
What is the refund window?

## Criteria
States 14 days.
"""
CASE_HOURS = "What are the opening hours?\n"
CASE_OFF = "---\nenabled: false\n---\nAn old question.\n"

SCHEMA1 = {
    "schema": 1, "hub": "h", "started_at": "2026-09-29T10:00:00+00:00",
    "finished_at": "2026-09-29T10:01:00+00:00", "model": "model-a",
    "judge_model": None, "judged": False, "passed": 1, "failed": 1,
    "cases": [
        {"name": "refund", "tags": ["canary"], "passed": False,
         "reason": "missing tool: read_knowledge", "duration": 2.5, "error": None,
         "checks": [{"kind": "expect_tools", "passed": False,
                     "detail": "missing tool: read_knowledge"}],
         "judge": None, "tool_calls": ["search"], "response": "Probably 30 days."},
        {"name": "hours", "tags": [], "passed": True, "reason": "", "duration": 1.0,
         "error": None, "checks": [], "judge": None, "tool_calls": [],
         "response": "9 to 5."},
    ],
}
SCHEMA2 = {
    "schema": 2, "hub": "h", "trigger": "console", "run_as": None,
    "started_at": "2026-09-30T10:00:00+00:00", "finished_at": "2026-09-30T10:02:00+00:00",
    "model": "model-b", "judge_model": "judge-x", "judged": True, "passed": 1, "failed": 0,
    "cases": [
        {"name": "refund", "tags": ["canary"], "passed": True, "reason": "",
         "duration": 3.25, "error": None,
         "checks": [{"kind": "expect_tools", "passed": True, "detail": ""}],
         "judge": {"score": 9, "threshold": 7, "reasoning": "Cites the policy.",
                   "model": "judge-x", "error": None},
         "tool_calls": ["read_knowledge", "http_get"],
         "tools": [
             {"name": "read_knowledge", "args": {"name": "refund-policy"}, "ok": True,
              "error": None, "duration_ms": 41, "preview": "Refunds within 14 days",
              "turn": 1},
             {"name": "http_get", "args": {"url": "https://x.test", "api_key": "sk-123",
                                           "headers": {"Authorization": "Bearer t"}},
              "ok": False, "error": "timed out", "duration_ms": 5000, "preview": None,
              "turn": 2},
         ],
         "turns": [{"prompt": "Hi", "response": "Hello."},
                   {"prompt": "Refund window?", "response": "14 days."}],
         "run_as": "ann@example.org",
         "response": "14 days."},
    ],
}


def _write_cases(hub: Path) -> None:
    (hub / "evals").mkdir(exist_ok=True)
    (hub / "evals" / "refund.md").write_text(CASE_REFUND)
    (hub / "evals" / "hours.md").write_text(CASE_HOURS)
    (hub / "evals" / "old.md").write_text(CASE_OFF)
    (hub / "evals" / "_draft.md").write_text("not a case")


def _write_run(hub: Path, stamp: str, data: dict) -> None:
    d = hub / ".hubzoid" / "evals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stamp}.json").write_text(json.dumps(data))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    hub = tmp_path / "evalhub"
    hub.mkdir()
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    for name in ("HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)
    access._stores.clear()
    gs = access.store_for(hub)
    gs.set_authoritative(True)
    gs.grant("root", "*", "manage_access", actor="test")
    admin = {"who": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(hub, admin_resolver=lambda _r: admin["who"]))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    c.gs, c.admin, c.hub_dir, c.key = gs, admin, hub, hub.name  # type: ignore[attr-defined]
    return c


def _api(path: str) -> str:
    return "/portal/api" + path


# ---- reading -----------------------------------------------------------------

def test_no_evals_folder_says_so(client):
    r = client.get(_api("/evals"), params={"hub": client.key})
    assert r.status_code == 200
    body = r.json()
    assert body["folder"] is False and body["cases"] == [] and body["runs"] == 0
    assert body["docs"].endswith("docs/evals.md")
    assert body["active"] is None and body["last"] is None
    assert client.get(_api("/evals/runs"), params={"hub": client.key}).json()["runs"] == []


def test_cases_with_their_latest_result(client):
    _write_cases(client.hub_dir)
    _write_run(client.hub_dir, "20260929_100000", SCHEMA1)
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    body = client.get(_api("/evals"), params={"hub": client.key}).json()
    assert body["folder"] is True and body["errors"] == [] and body["runs"] == 2
    rows = {c["name"]: c for c in body["cases"]}
    assert set(rows) == {"refund", "hours", "old"}          # _draft.md is a note
    refund = rows["refund"]
    assert refund["tags"] == ["canary"] and refund["schedule"] == "0 6 * * 1"
    assert refund["judged"] is True and refund["turns"] == 1 and refund["enabled"] is True
    assert refund["checks"] == ["expects read_knowledge"]
    assert refund["prompt"] == "What is the refund window?"
    # The newest file that ran each case: refund in the schema 2 run, hours only
    # in the older schema 1 run.
    assert refund["latest"] == {"stamp": "20260930_100000", "passed": True, "reason": "",
                                "finished": "2026-09-30T10:02:00+00:00", "trigger": "console"}
    assert rows["hours"]["latest"]["stamp"] == "20260929_100000"
    assert rows["hours"]["latest"]["trigger"] is None and rows["hours"]["judged"] is False
    assert rows["old"]["enabled"] is False and rows["old"]["latest"] is None


def test_case_row_carries_multi_turn_and_run_as():
    """Fields evals-core adds to EvalCase (turns, run_as, expect_tool_args)."""
    case = SimpleNamespace(
        name="chat", tags=[], schedule=None, is_judged=False, enabled=True,
        prompt="Hi", turns=["Hi", "And the refund?"], run_as="ann@example.org",
        expect_tools=[], forbid_tools=[], contains=[], not_contains=[],
        expect_tool_args={"read_knowledge": {"name": "refund-policy"}})
    row = portal_evals._case_row(case)
    assert row["turns"] == 2 and row["run_as"] == "ann@example.org"
    assert row["checks"] == ["expects read_knowledge with name"]


def test_a_broken_case_file_is_listed_with_its_error(client):
    _write_cases(client.hub_dir)
    (client.hub_dir / "evals" / "broken.md").write_text("---\nexpected_tools: [x]\n---\nQ\n")
    body = client.get(_api("/evals"), params={"hub": client.key}).json()
    assert [c["name"] for c in body["cases"]] == ["hours", "old", "refund"]
    assert body["errors"][0]["file"] == "evals/broken.md"
    assert "unknown frontmatter" in body["errors"][0]["error"]


def test_runs_newest_first_from_schema_1_and_2(client):
    _write_run(client.hub_dir, "20260929_100000", SCHEMA1)
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    _write_run(client.hub_dir, "20260930_110000", {"cases": "not a list"})
    (client.hub_dir / ".hubzoid" / "evals" / "20260930_120000.json").write_text("{nope")
    runs = client.get(_api("/evals/runs"), params={"hub": client.key}).json()["runs"]
    stamps = [r["stamp"] for r in runs]
    assert stamps[-2:] == ["20260930_100000", "20260929_100000"]
    assert "20260930_120000" not in stamps                  # unreadable: skipped
    newer, older = runs[-2], runs[-1]
    assert newer == {"stamp": "20260930_100000", "schema": 2, "trigger": "console",
                     "started": "2026-09-30T10:00:00+00:00",
                     "finished": "2026-09-30T10:02:00+00:00", "model": "model-b",
                     "judge_model": "judge-x", "judged": True, "run_as": None,
                     "passed": 1, "failed": 0, "total": 1}
    assert older["schema"] == 1 and older["trigger"] is None
    assert (older["passed"], older["failed"], older["total"]) == (1, 1, 2)


def test_run_detail_schema_2(client):
    _write_cases(client.hub_dir)
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    r = client.get(_api("/evals/runs/20260930_100000"), params={"hub": client.key})
    assert r.status_code == 200
    body = r.json()
    assert body["trigger"] == "console" and body["model"] == "model-b"
    case = body["cases"][0]
    assert case["passed"] is True and case["duration"] == 3.25
    assert case["judge"] == {"score": 9, "threshold": 7, "reasoning": "Cites the policy.",
                             "model": "judge-x", "error": None, "passed": True}
    assert case["checks"] == [{"kind": "expect_tools", "passed": True, "detail": ""}]
    assert case["answer"] == "14 days." and case["run_as"] == "ann@example.org"
    assert case["prompt"] == "What is the refund window?"   # from the case file now
    assert case["turns"] == [{"prompt": "Hi", "response": "Hello."},
                             {"prompt": "Refund window?", "response": "14 days."}]
    first, second = case["tools"]
    assert first == {"name": "read_knowledge", "args": {"name": "refund-policy"}, "ok": True,
                     "error": None, "duration_ms": 41, "preview": "Refunds within 14 days",
                     "turn": 1}
    # Secret-looking argument values never leave the server.
    assert second["args"] == {"url": "https://x.test", "api_key": "[redacted]",
                              "headers": {"Authorization": "[redacted]"}}
    assert second["ok"] is False and second["error"] == "timed out" and second["turn"] == 2


def test_run_detail_schema_1(client):
    _write_run(client.hub_dir, "20260929_100000", SCHEMA1)
    body = client.get(_api("/evals/runs/20260929_100000"), params={"hub": client.key}).json()
    assert body["schema"] == 1 and body["trigger"] is None
    refund, hours = body["cases"]
    assert refund["passed"] is False and refund["reason"] == "missing tool: read_knowledge"
    assert refund["tools"] == [{"name": "search", "args": None, "ok": None, "error": None,
                                "duration_ms": None, "preview": None, "turn": 1}]
    assert refund["turns"] is None and refund["judge"] is None and refund["prompt"] is None
    assert hours["answer"] == "9 to 5."


@pytest.mark.parametrize("stamp", ["...", "a.b", "-x", "x" * 81, "20260930 1", "a%00b",
                                   "20260930_100000.json"])
def test_bad_run_ids_are_refused(client, stamp):
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    r = client.get(_api(f"/evals/runs/{stamp}"), params={"hub": client.key})
    assert r.status_code == 422 and r.json()["code"] == "invalid_run"


def test_the_run_id_pattern_admits_stamps_only():
    assert portal_evals.STAMP_RE.match("20260930_100000")
    for bad in ("..", ".", "../x", "a/b", "a\\b", "", ".hidden", "x.json"):
        assert not portal_evals.STAMP_RE.match(bad), bad


def test_path_traversal_never_reads_outside_the_results_folder(client):
    (client.hub_dir / "secret.json").write_text(json.dumps(SCHEMA2))
    for stamp in ("..%2F..%2Fsecret", "..%2Fsecret", "%2e%2e%2fsecret"):
        r = client.get("/portal/api/evals/runs/" + stamp, params={"hub": client.key})
        assert r.status_code in (404, 422), stamp
        assert "refund" not in r.text


def test_unknown_run_is_404(client):
    r = client.get(_api("/evals/runs/20200101_000000"), params={"hub": client.key})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


# ---- who may see and run ----------------------------------------------------

ROUTES = [("get", "/evals", None), ("get", "/evals/runs", None),
          ("get", "/evals/runs/20260930_100000", None), ("post", "/evals/run", True)]


@pytest.mark.parametrize("method,path,post", ROUTES)
def test_someone_without_console_access_gets_403(client, method, path, post):
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    client.admin["who"] = None
    if post:
        r = client.post(_api(path), json={"hub": client.key, "confirm": True})
    else:
        r = getattr(client, method)(_api(path), params={"hub": client.key})
    assert r.status_code == 403


@pytest.mark.parametrize("method,path,post", ROUTES)
def test_a_manager_of_another_hub_gets_403(client, method, path, post):
    _write_run(client.hub_dir, "20260930_100000", SCHEMA2)
    client.admin["who"] = PortalAdmin(subject="mgr@example.org", is_org_admin=False,
                                      manageable=["finance"])
    if post:
        r = client.post(_api(path), json={"hub": client.key, "confirm": True})
    else:
        r = getattr(client, method)(_api(path), params={"hub": client.key})
    assert r.status_code == 403


def test_this_hubs_manager_may_see_evals(client):
    _write_cases(client.hub_dir)
    client.admin["who"] = PortalAdmin(subject="mgr@example.org", is_org_admin=False,
                                      manageable=[client.key])
    assert client.get(_api("/evals"), params={"hub": client.key}).status_code == 200


@pytest.mark.parametrize("method,path,post", ROUTES)
def test_unknown_hub_is_404(client, method, path, post):
    if post:
        r = client.post(_api(path), json={"hub": "nope", "confirm": True})
    else:
        r = getattr(client, method)(_api(path), params={"hub": "nope"})
    assert r.status_code == 404


# ---- starting a run -----------------------------------------------------------

@pytest.fixture()
def queued(client, monkeypatch):
    """Record what would be queued; the engine is reported running."""
    calls = []

    def fake_start(hub_path, *, names, judge, requested_by):
        calls.append(dict(hub_path=hub_path, names=names, judge=judge,
                          requested_by=requested_by))
        return "evals:console:20260930T100000-abc123@hz-x"

    monkeypatch.setattr(portal_evals, "start_run", fake_start)
    monkeypatch.setattr(portal_evals, "engine_problem", lambda *a: None)
    _write_cases(client.hub_dir)
    return calls


def test_starting_a_run_needs_explicit_confirmation(client, queued):
    for body in ({"hub": client.key}, {"hub": client.key, "confirm": False}):
        r = client.post(_api("/evals/run"), json=body)
        assert r.status_code == 422 and r.json()["code"] == "confirm_required"
        assert "model calls" in r.json()["detail"]
    assert queued == []


def test_a_cross_site_start_is_refused(client, queued):
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and queued == []


def test_start_all_enabled_cases_queues_and_audits(client, queued):
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True and body["run_id"].startswith("evals:console:")
    assert body["cases"] == ["hours", "refund"] and body["judge"] is True
    assert queued == [dict(hub_path=client.hub_dir, names=None, judge=True,
                           requested_by="root")]
    rows = client.gs.read_access_audit(10, hubs=[client.key])
    assert any(r["action"] == "evals_run" and r["actor"] == "root"
               and r["permission"] == body["run_id"] for r in rows)


def test_start_selected_cases_without_the_judge(client, queued):
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True,
                                              "cases": ["refund", "refund"], "judge": False})
    assert r.status_code == 202
    assert queued[0]["names"] == ["refund"] and queued[0]["judge"] is False


@pytest.mark.parametrize("cases,code", [(["nope"], "unknown_case"), (["old"], "disabled_case"),
                                        ([], "no_cases")])
def test_start_refuses_bad_selections(client, queued, cases, code):
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True,
                                              "cases": cases})
    assert r.status_code == 422 and r.json()["code"] == code
    assert queued == []


def test_start_refuses_while_a_case_file_is_broken(client, queued):
    (client.hub_dir / "evals" / "broken.md").write_text("---\ntimeout: soon\n---\nQ\n")
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True})
    assert r.status_code == 422 and r.json()["code"] == "bad_cases"
    assert r.json()["errors"][0]["file"] == "evals/broken.md" and queued == []


def test_start_refuses_without_cases(client, monkeypatch):
    monkeypatch.setattr(portal_evals, "engine_problem", lambda *a: None)
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True})
    assert r.status_code == 422 and r.json()["code"] == "no_cases"


def test_start_refuses_when_the_engine_is_off(client):
    _write_cases(client.hub_dir)
    client.gs.set_runtime_health(client.hub_dir.name, enabled=False, error=None)
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True})
    assert r.status_code == 409 and r.json()["code"] == "engine_off"
    assert "HUBZOID_DISABLE_SCHEDULE" in r.json()["detail"]


def test_unknown_fields_are_refused(client, queued):
    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True,
                                              "run_as": "someone@example.org"})
    assert r.status_code == 422 and queued == []


# ---- against a real DBOS database ------------------------------------------

def _clean_env(tmp_path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT",
                        "HUBZOID_OPERATIONAL_DB", "HUBZOID_DISABLE_SCHEDULE")}
    env["PYTHONPATH"] = str(ROOT)
    return env


_CREATE_SCHEMA = textwrap.dedent('''
    import sys
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    runtime.shutdown()
    print("OK")
''')


def test_a_second_start_is_refused_while_one_is_queued(client, tmp_path):
    """The real DBOS client: the first start queues `hz_eval_console` on the
    hub's markdown queue (no bridge is running, so it stays queued), the run
    shows as active, and a second start gets 409 naming it."""
    _write_cases(client.hub_dir)
    proc = subprocess.run([sys.executable, "-c", _CREATE_SCHEMA, str(client.hub_dir)],
                          capture_output=True, text=True, timeout=180, env=_clean_env(tmp_path))
    assert "OK" in proc.stdout, proc.stderr[-2000:]
    client.gs.set_runtime_health(client.hub_dir.name, enabled=True)

    r = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True,
                                              "cases": ["refund"], "judge": False})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]

    active = client.get(_api("/evals"), params={"hub": client.key}).json()["active"]
    assert active["id"] == run_id and active["status"] == "ENQUEUED"
    assert active["source"] == "console" and active["requested_by"] == "root"
    assert active["cases"] == ["refund"] and active["judge"] is False

    again = client.post(_api("/evals/run"), json={"hub": client.key, "confirm": True})
    assert again.status_code == 409
    assert again.json()["code"] == "eval_running" and again.json()["run_id"] == run_id

    from dbos import DBOSClient

    from hubzoid.workflows import markdown
    from hubzoid.workflows.runtime import _app_name

    c = DBOSClient(system_database_url=db.dbos_url(client.hub_dir),
                   application_name=_app_name(client.hub_dir.name))
    try:
        (w,) = c.list_workflows(workflow_ids=[run_id])
        assert w.name == markdown.EVAL_CONSOLE_WORKFLOW
        assert w.queue_name == markdown.queue_name(client.hub_dir.name)
    finally:
        c.destroy()


_E2E = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    hub = Path(sys.argv[1])
    calls = []

    import hubzoid.evals as evals
    from hubzoid.evals import report
    from hubzoid.evals.results import CaseResult, SuiteResult

    def fake_run_and_save(hub_dir: Path, names: list[str] | None = None, *, judge: bool = True,
                          run_as: str | None = None, trigger: str = "cli",
                          model: str | None = None):
        calls.append(dict(hub_dir=str(hub_dir), names=names, judge=judge, run_as=run_as,
                          trigger=trigger, model=model))
        suite = SuiteResult(hub=Path(hub_dir).name, cases=[CaseResult(name="refund")],
                            started_at="2026-09-30T10:00:00+00:00",
                            finished_at="2026-09-30T10:00:01+00:00", judged=judge)
        return report.save(Path(hub_dir), suite, stamp=f"20260930_10000{len(calls)}"), suite

    evals.run_and_save = fake_run_and_save

    from hubzoid import access
    from hubzoid.portal import PortalAdmin, build_router
    from hubzoid.workflows import markdown, observe, runtime

    gs = access.store_for(hub)
    gs.set_authoritative(True)
    gs.grant("root", "*", "manage_access", actor="test")
    runtime.init(hub)
    runtime.launch()
    gs.set_runtime_health(hub.name, enabled=True)
    app = FastAPI()
    app.include_router(build_router(hub, admin_resolver=lambda r: PortalAdmin("root", True, [])))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})

    def run(body):
        r = c.post("/portal/api/evals/run", json=dict(body, hub=hub.name, confirm=True))
        assert r.status_code == 202, r.text
        deadline = time.time() + 90
        while time.time() < deadline:
            state = c.get("/portal/api/evals", params={"hub": hub.name}).json()
            if state["active"] is None and state["last"] and state["last"]["id"] == r.json()["run_id"]:
                return state
            time.sleep(0.3)
        raise SystemExit("TIMEOUT " + json.dumps(state))

    out = {}
    out["first"] = run({"cases": ["refund"], "judge": False})
    out["runs"] = c.get("/portal/api/evals/runs", params={"hub": hub.name}).json()["runs"]
    out["view"] = observe.runs(hub, name="evals", viewer="someone-else@example.org")
    # A run interrupted by a restart is never repeated: its marker is already set.
    from hubzoid import db
    from hubzoid.workflows.state import WorkflowState

    fixed = "evals:console:fixed@" + markdown.hub_namespace(hub.name)
    markdown.console_eval_run_id = lambda *a, **k: fixed
    WorkflowState(db.operational_engine(hub), hub.name, "evals:console")["started:" + fixed] = "started"
    out["interrupted"] = run({})
    out["calls"] = calls
    runtime.shutdown()
    print("RESULT " + json.dumps(out, default=str))
''')


def test_a_console_run_executes_on_dbos_and_calls_run_and_save(tmp_path):
    hub = tmp_path / "e2ehub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: e2ehub\ndescription: d\n---\nbody")
    _write_cases(hub)
    proc = subprocess.run([sys.executable, "-c", _E2E, str(hub)], capture_output=True,
                          text=True, timeout=240, env=_clean_env(tmp_path))
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, (proc.stdout[-2000:], proc.stderr[-4000:])
    out = json.loads(line[7:])

    # The workflow step called run_and_save exactly once, for the Console.
    assert out["calls"] == [dict(hub_dir=str(hub), names=["refund"], judge=False,
                                 run_as=None, trigger="console", model=None)]
    last = out["first"]["last"]
    assert last["status"] == "SUCCESS" and last["source"] == "console"
    assert last["stamp"] == "20260930_100001" and last["requested_by"] == "root"
    assert out["runs"][0]["stamp"] == "20260930_100001"
    assert out["first"]["cases"][[c["name"] for c in out["first"]["cases"]].index("refund")][
        "latest"]["stamp"] == "20260930_100001"
    # Listed with the hub's other runs, as "evals", its summary visible to the
    # hub's managers (it holds no one's data).
    (row,) = out["view"]
    assert row["name"] == "evals" and row["status"] == "SUCCESS"
    assert "20260930_100001" in (row["output"] or "") and row["redacted"] is False
    # The interrupted run failed with its reason and made no model call.
    bad = out["interrupted"]["last"]
    assert bad["status"] == "ERROR" and "interrupted by a restart" in bad["error"]


# ---- the hub's engine starts for eval cases ---------------------------------

def test_eval_cases_start_the_workflow_engine(tmp_path, monkeypatch):
    from hubzoid.workflows import boot

    monkeypatch.delenv("HUBZOID_DISABLE_SCHEDULE", raising=False)
    assert boot.eval_work(tmp_path) is False                     # no evals folder
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "_draft.md").write_text("a note")
    assert boot.eval_work(tmp_path) is False                     # notes are not cases
    (tmp_path / "evals" / "refund.md").write_text(CASE_REFUND)
    assert boot.eval_work(tmp_path) is True
    assert boot.eval_work(tmp_path, env={"HUBZOID_DISABLE_SCHEDULE": "1"}) is False


def test_an_evals_only_hub_gets_its_engine(tmp_path, monkeypatch):
    import asyncio

    from hubzoid.workflows import boot

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    for name in ("HUBZOID_SCHEDULES", "HUBZOID_GATEWAY", "HUBZOID_DISABLE_SCHEDULE"):
        monkeypatch.delenv(name, raising=False)
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    (hub / "evals" / "hours.md").write_text(CASE_HOURS)     # manual case, no schedule
    monkeypatch.setattr(boot.Dispatcher, "prepare", lambda self: 0)
    disp = asyncio.run(boot.start(hub))
    assert disp is not None
    assert access.store_for(hub).runtime_health(hub.name)["enabled"] is True


def test_a_console_run_queued_before_its_bridge_started_is_requeued(monkeypatch):
    """Queued without a code version, it is re-queued under the bridge's code
    rather than left to block the queue."""
    from hubzoid.workflows import markdown, runtime

    queued = []
    monkeypatch.setattr(runtime, "_MD_QUEUE",
                        SimpleNamespace(enqueue=lambda fn, *args: queued.append((fn, args))))
    monkeypatch.setitem(markdown._FNS, "eval_console", "eval-console-fn")
    w = SimpleNamespace(status="ENQUEUED", name=markdown.EVAL_CONSOLE_WORKFLOW,
                        workflow_id="evals:console:x@hz", input={"args": [["refund"], False, "root"]})
    assert runtime._requeue_markdown(w) is True
    assert queued == [("eval-console-fn", (["refund"], False, "root"))]


def test_eval_runs_read_as_evals_in_the_runs_view():
    from hubzoid.workflows import markdown, observe

    w = SimpleNamespace(name=markdown.EVAL_CONSOLE_WORKFLOW, workflow_id="evals:console:x@hz",
                        status="SUCCESS", created_at=1, dequeued_at=2, completed_at=5,
                        output={"stamp": "20260930_100000"}, error=None)
    assert observe._run_row("h", w)["name"] == "evals"
    assert observe._name_filter("evals") == (list(markdown.EVAL_WORKFLOWS), None)
    assert observe._name_filter("md:sync") == (markdown.MD_WORKFLOW, "md:sync:")
    assert observe._name_filter("nightly") == ("nightly", None)
    assert observe._hub_level(markdown.EVAL_WORKFLOW) and not observe._hub_level("nightly")
