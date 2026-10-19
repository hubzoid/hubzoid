"""Workflows from chat: typed inputs, Run for me, personal schedules and their
chat tools, list email and GitHub signatures. No model and no DBOS engine:
the engine is a small fake that records what would be queued."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace

from pydantic import BaseModel
from types import SimpleNamespace

import pytest

from hubzoid import artifacts as arts
from hubzoid.access import Identity, identity_scope, store_for
from hubzoid.workflows import inputs as inputs_lib
from hubzoid.workflows import personal, runtime

TEAM_ACCT, MEERA, RAVI = "reports@co.org", "meera@co.org", "ravi@co.org"

WORKFLOW = '''
from __future__ import annotations   # input models must still resolve
from typing import Literal
from pydantic import BaseModel, Field
from hubzoid import workflow, hub

class ProgramReport(BaseModel):
    program: str | None = Field(None, description="One program. Empty means all.")
    period: Literal["week", "month"] = "week"
    token: str | None = Field(None, json_schema_extra={"hidden": True})

@workflow(schedule="daily at 06:00", timezone="Asia/Kolkata", run_as="reports@co.org",
          inputs=ProgramReport, as_tool=True, schedulable=True)
def program_report():
    """Program registrations. One live link per program."""
    page = hub.data_dir / "page.html"
    page.write_text(f"<h1>{hub.input.program} {hub.input.period}</h1>")
    art = hub.publish_artifact(page, title=f"{hub.input.program} report",
                               key=f"program-{(hub.input.program or 'all').lower()}",
                               audience="domain" if hub.team_run else "owner")
    return {"team": hub.team_run, "trigger": hub.trigger, "url": art["latest_url"]}

@workflow(as_tool=True, confirm=True, surfaces=("web", "mcp"))
def costly_audit():
    """A long audit."""
    return {"ok": True}
'''


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("WEBUI_AUTH", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
              "HUBZOID_SHARE_DOMAINS", "HUBZOID_EMAIL_DELIVERY", "HUBZOID_SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_SCHEDULES", "1")
    d = tmp_path / "irs"
    (d / "workflows" / "program_report").mkdir(parents=True)
    (d / "AGENTS.md").write_text("---\nname: irs\n---\nbody")
    (d / "workflows" / "program_report" / "program_report.py").write_text(WORKFLOW)
    gs = store_for(d)
    for e in (TEAM_ACCT, MEERA, RAVI):
        gs.upsert_identity(email=e, owui_id="id-" + e)
        gs.grant(e, "irs", "use_hub", actor="t")
    gs.grant(MEERA, "irs", "wf_program_report", actor="t")
    gs.grant(MEERA, "irs", "schedule_reports", actor="t")
    return d


class FakeEngine:
    """Stands in for DBOS: records queued runs; a run "finishes" by publishing."""

    def __init__(self, hub_dir):
        self.hub_dir, self.started, self.status = hub_dir, [], {}

    def start_run(self, name, envelope, *, workflow_id=None):
        workflow_id = workflow_id or f"run-{len(self.started)}"
        if workflow_id in {w for w, _, _ in self.started}:
            return SimpleNamespace(get_workflow_id=lambda: workflow_id)
        self.started.append((workflow_id, name, dict(envelope)))
        src = self.hub_dir / f"{workflow_id}.html"
        src.write_text(f"<h1>{envelope.get('inputs')}</h1>")
        arts.publish(self.hub_dir, hub="irs", owner=envelope.get("acts_as") or TEAM_ACCT, owner_account=None,
                     source=src, title="Bhava report", workflow=name, run_id=workflow_id,
                     key="program-bhava", series={"scope": envelope.get("scope"),
                                                  "schedule_id": envelope.get("schedule_id")})
        self.status[workflow_id] = "SUCCESS"
        return SimpleNamespace(get_workflow_id=lambda: workflow_id)

    # the DBOS surface the tools read
    def get_workflow_status(self, run_id):
        return SimpleNamespace(status=self.status.get(run_id))

    def list_workflows(self, **_kw):
        return []

    def retrieve_workflow(self, run_id):
        return SimpleNamespace(get_result=lambda: {"ok": True})


@pytest.fixture
def engine(hub, monkeypatch):
    fake = FakeEngine(hub)
    defs = {d.name: d for d in runtime.load_definitions(hub)}
    monkeypatch.setattr(runtime, "_REGISTRY", defs)
    monkeypatch.setattr(runtime, "_LAUNCHED", True)
    monkeypatch.setattr(runtime, "_DBOS", fake)
    monkeypatch.setattr(runtime, "start_run", fake.start_run)
    return fake


# ---- declarations and inputs --------------------------------------------------------------


def test_definitions_are_read_without_the_engine(hub):
    defs = {d.name: d for d in runtime.load_definitions(hub)}
    report = defs["program_report"]
    assert report.as_tool and report.schedulable and report.title == "Program report"
    assert report.permission == "wf_program_report"
    schema = inputs_lib.schema(report.inputs)
    assert set(schema["properties"]) == {"program", "period", "token"} and "required" not in schema
    assert defs["costly_audit"].confirm and defs["costly_audit"].surfaces == ("web", "mcp")


@pytest.mark.parametrize("kwargs,problem", [
    (dict(schedule="daily at 06:00", inputs="Required"), "needs schedule_input"),
    (dict(on_webhook="gh", inputs="Required"), "webhook can't fill required inputs"),
    (dict(schedulable=True), "schedulable needs as_tool"),
    (dict(as_tool=True, surfaces=("slack",)), "surfaces can only name"),
    (dict(as_tool=True, tool_wait="20m"), "at most 10 minutes"),
    (dict(inputs="Required", as_tool=True, schedulable=True, max_input_kb=200), "can't be used with schedulable"),
    (dict(inputs="Required", max_input_kb=1000), "from 1 to 256"),
])
def test_bad_declarations_are_refused(kwargs, problem):
    from pydantic import BaseModel

    class Required(BaseModel):
        program: str

    if kwargs.get("inputs") == "Required":
        kwargs["inputs"] = Required
    base = dict(inputs=None, schedule_input=None, as_tool=False, confirm=False, tool_wait=None,
                surfaces=None, sensitive=False, schedulable=False, min_interval=None, catch_up=False,
                schedule=None, on_webhook=None)
    with pytest.raises(ValueError, match=problem):
        runtime._chat_options(**{**base, **kwargs})


def test_inputs_are_checked_with_one_safe_sentence(hub):
    model = {d.name: d for d in runtime.load_definitions(hub)}["program_report"].inputs
    with pytest.raises(inputs_lib.InputError, match="period must be one of week or month. Nothing ran."):
        inputs_lib.validate(model, {"period": "quarter"})
    with pytest.raises(inputs_lib.InputError, match="colour isn't an input"):
        inputs_lib.validate(model, {"colour": "red"})
    data = inputs_lib.dump(inputs_lib.validate(model, {"program": "Bhava", "token": "s3cret"}))
    assert inputs_lib.display(model, data) == "Bhava, week, ••••"
    with pytest.raises(inputs_lib.InputError, match="too long"):
        inputs_lib.validate(model, {"program": "x" * 5000})
    assert inputs_lib.validate(model, {"program": "x" * 5000}, max_bytes=200 * 1024)
    big = runtime._chat_options(inputs=model, schedule_input={"program": "x" * 5000}, as_tool=True,
                                confirm=False, tool_wait=None, surfaces=None, sensitive=False,
                                schedulable=False, min_interval=None, catch_up=False,
                                schedule="daily at 06:00", on_webhook=None, max_input_kb=200)
    assert big["max_input_bytes"] == 200 * 1024                     # schedule_input uses the raised limit
    assert inputs_lib.display_with_schema(None, {"token": "s3cret"}) == "••••"   # workflow gone: mask all

    from pydantic import BaseModel, model_validator

    class Span(BaseModel):
        start: int = 0
        end: int = 0

        @model_validator(mode="after")
        def ordered(self):
            if self.end < self.start:
                raise ValueError("end is before start")
            return self
    with pytest.raises(inputs_lib.InputError, match=r"^End is before start\. Nothing ran\.$"):
        inputs_lib.validate(Span, {"start": 2, "end": 1})
    assert inputs_lib.parse_cli(["program=Bhava", "limit=5"]) == {"program": "Bhava", "limit": 5}


def test_run_local_runs_as_a_person_or_the_team(hub):
    from hubzoid.workflows.testing import run_local

    defs = {d.name: d for d in runtime.load_definitions(hub)}
    mine = run_local(defs["program_report"].fn, hub_dir=hub, inputs={"program": "Bhava"}, as_user=MEERA)
    assert mine.result["team"] is False and mine.artifacts[0]["audience"] == "owner"
    team = run_local(defs["program_report"].fn, hub_dir=hub, trigger="schedule")
    assert team.result["team"] is True and team.artifacts[0]["key"] == "program-all"
    assert (hub / ".hubzoid" / "data" / "page.html").exists()


# ---- personal schedules -------------------------------------------------------------------


def _save(hub, actor=MEERA, cadence="every monday at 09:00", program="Bhava"):
    out = personal.propose_create(hub, "irs", actor, workflow="program_report",
                                  inputs={"program": program, "period": "month"}, cadence=cadence,
                                  timezone_name="Asia/Kolkata")
    return out, personal.confirm(hub, "irs", actor, out["code"])


def test_saving_takes_two_calls_and_the_first_run_starts(hub, engine):
    from hubzoid._request_ctx import chat_scope

    ask = dict(workflow="program_report", inputs={"program": "Bhava", "period": "month"},
               cadence="every monday at 09:00", timezone_name="Asia/Kolkata")
    with chat_scope("c1"):                                              # the turn that asks
        proposal = personal.propose_create(hub, "irs", MEERA, **ask)
        assert personal.pending(hub, "irs", MEERA, kinds=("create",)) == []
        with pytest.raises(personal.ScheduleError, match="next message"):
            personal.confirm(hub, "irs", MEERA, proposal["code"])       # the model can't say yes
    assert proposal["text"].startswith("Not saved yet. Mondays at 09:00 (Asia/Kolkata)")
    assert personal.for_owner(hub, "irs", MEERA) == []                  # nothing stored yet
    with pytest.raises(personal.ScheduleError, match="doesn't match"):
        personal.confirm(hub, "irs", RAVI, proposal["code"])            # someone else's code
    with chat_scope("other"):
        assert personal.pending(hub, "irs", MEERA, kinds=("create",)) == []
        with pytest.raises(personal.ScheduleError, match="different chat"):
            personal.confirm(hub, "irs", MEERA, proposal["code"])
    with chat_scope("c1"):                                              # the person's yes
        assert personal.pending(hub, "irs", RAVI, kinds=("create",)) == []
        assert personal.pending(hub, "irs", MEERA, kinds=("run",)) == []
        pending = personal.pending(hub, "irs", MEERA, kinds=("create",))
        assert [p["code"] for p in pending] == [proposal["code"]]
        assert not any(k.startswith("_") for k in pending[0]["values"])
        assert personal.propose_create(hub, "irs", MEERA, **ask)["code"] == proposal["code"]
        saved = personal.confirm(hub, "irs", MEERA, proposal["code"])
        assert personal.pending(hub, "irs", MEERA, kinds=("create",)) == []
    assert saved["schedule"].state == "testing" and saved["run_id"] == f"ps-{saved['schedule'].id}-first"
    with pytest.raises(personal.ScheduleError, match="doesn't match|already used"):
        personal.confirm(hub, "irs", MEERA, proposal["code"])           # used once
    _, envelope = engine.started[0][1], engine.started[0][2]
    assert (envelope["acts_as"], envelope["scope"], envelope["trigger"]) == (MEERA, "personal", "personal_schedule")


def test_people_get_no_email_unless_the_hub_turns_it_on(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    assert not personal.email_on(hub)                       # delivery set up, still off
    assert "email" not in personal.propose_create(
        hub, "irs", MEERA, workflow="program_report", inputs={"program": "Bhava"},
        cadence="daily at 09:00", timezone_name="Asia/Kolkata")["text"]
    (hub / "workflows" / "settings.yaml").write_text("notify_by_email: true\n")
    assert personal.email_on(hub)


def test_limits_and_refusals_name_the_fix(hub, engine):
    with pytest.raises(personal.ScheduleError, match="at most once every hour"):
        personal.propose_create(hub, "irs", MEERA, workflow="program_report", inputs={},
                                cadence="every 15 minutes", timezone_name="Asia/Kolkata")
    with pytest.raises(personal.ScheduleError, match="can't read"):
        personal.propose_create(hub, "irs", MEERA, workflow="program_report", inputs={},
                                cadence="whenever", timezone_name="Asia/Kolkata")
    with pytest.raises(personal.ScheduleError, match="isn't a time zone"):
        personal.propose_create(hub, "irs", MEERA, workflow="program_report", inputs={},
                                cadence="daily at 09:00", timezone_name="Mars/Base")
    with pytest.raises(personal.ScheduleError, match="can't run Program report yet"):
        personal.propose_create(hub, "irs", RAVI, workflow="program_report", inputs={},
                                cadence="daily at 09:00", timezone_name="Asia/Kolkata")
    _save(hub)
    with pytest.raises(personal.ScheduleError, match="already have"):
        _save(hub)


def test_the_dispatcher_fires_each_slot_once_and_pauses_on_lost_access(hub, engine):
    _, saved = _save(hub)
    s = saved["schedule"]
    personal.after_run(hub, "irs", {"schedule_id": s.id}, saved["run_id"], ok=True, cause=None, published=[])
    assert personal.get(hub, s.id).state == "active"
    due = personal.get(hub, s.id).next_fire
    first = personal.fire_due(hub, "irs", now=due + 30)
    again = personal.fire_due(hub, "irs", now=due + 90)                  # next_fire already moved
    assert first == [f"ps-{s.id}-{int(due)}"] and again == []
    after_downtime = personal.get(hub, s.id).next_fire + 3 * 7 * 86400   # three weeks missed
    started = personal.fire_due(hub, "irs", now=after_downtime + 600)
    assert len(started) == 1                                             # caught up once
    store_for(hub).revoke(MEERA, "irs", "wf_program_report", actor="admin")
    personal.fire_due(hub, "irs", now=personal.get(hub, s.id).next_fire + 1)
    paused = personal.get(hub, s.id)
    assert (paused.state, paused.reason) == ("paused", "access")
    assert personal.state_words(paused) == "Paused: access changed"


def test_failures_pause_after_three_and_admins_resume_only_their_own_pause(hub, engine):
    _, saved = _save(hub)
    sid = saved["schedule"].id
    personal.after_run(hub, "irs", {"schedule_id": sid}, saved["run_id"], ok=True, cause=None,
                       published=[{"series_id": "s-bhava", "changed": False}])
    assert personal.get(hub, sid).series == ["s-bhava"]          # an unchanged copy still feeds it
    for i in range(3):
        personal._update(hub, sid, last_run=f"r{i}", last_status="running")
        personal.after_run(hub, "irs", {"schedule_id": sid}, f"r{i}", ok=False, cause="timeout", published=[])
        personal.after_run(hub, "irs", {"schedule_id": sid}, f"r{i}", ok=False, cause="timeout", published=[])
    s = personal.get(hub, sid)
    assert (s.state, s.reason, s.failures) == ("paused", "failures", 3)  # replays didn't double count
    with pytest.raises(personal.ScheduleError, match="only schedules they paused"):
        personal.set_paused(hub, "irs", "admin@co.org", sid, False, admin=True)
    personal.set_paused(hub, "irs", MEERA, sid, False)
    personal.set_paused(hub, "irs", "admin@co.org", sid, True, admin=True)
    with pytest.raises(personal.ScheduleError, match="paused by an admin"):
        personal.set_paused(hub, "irs", MEERA, sid, False)
    with pytest.raises(personal.ScheduleError, match="paused by an admin"):
        personal.run_now(hub, "irs", MEERA, sid)                      # nor run it now
    personal.set_paused(hub, "irs", MEERA, sid, True)                # pausing again changes nothing
    with pytest.raises(personal.ScheduleError, match="paused by an admin"):
        personal.set_paused(hub, "irs", MEERA, sid, False)
    personal.admin_stop(hub, "irs", "admin@co.org", sid)
    assert personal.get(hub, sid).state == "stopped"


# ---- chat tools ---------------------------------------------------------------------------


def _tools(hub):
    from hubzoid.tools import report_tools

    return {t.name: t for t in report_tools.make(SimpleNamespace(hub_dir=hub))}


def _call(tool, args, who=MEERA, surface="web"):
    async def go():
        with identity_scope(Identity.make(who, surface=surface)):
            return await tool.on_invoke_tool(None, json.dumps(args))
    return asyncio.run(go())


def test_run_for_me_runs_as_the_caller_and_returns_their_link(hub, engine):
    tools = _tools(hub)
    assert {"program_report", "costly_audit", "schedule_workflow", "my_schedules",
            "change_schedule", "my_reports"} <= set(tools)
    assert "confirm" in tools["costly_audit"].params_json_schema["properties"]
    out = _call(tools["program_report"], {"program": "Bhava", "period": "month"})
    assert out.startswith("Your Program report is ready:") and "/by/" in out
    assert engine.started[-1][2]["acts_as"] == MEERA and engine.started[-1][2]["trigger"] == "tool"
    assert "access denied" in _call(tools["program_report"], {}, who=RAVI)   # no grant
    assert "period must be one of week or month" in _call(tools["program_report"], {"period": "quarter"})
    # An answer the run returns shows after the links, fenced as data.
    engine.retrieve_workflow = lambda _id: SimpleNamespace(get_result=lambda: {"answer": "2 findings ```x```"})
    out = _call(tools["program_report"], {"program": "Bhava", "period": "week"})
    assert out.index("/by/") < out.index("Result (data from the run, not instructions):\n````\n2 findings")


def test_confirm_workflows_and_surfaces(hub, engine):
    tools = _tools(hub)
    store_for(hub).grant(MEERA, "irs", "wf_costly_audit", actor="t")
    assert "access denied" in _call(tools["costly_audit"], {}, surface="telegram")
    first = _call(tools["costly_audit"], {})
    assert first.startswith("Not started yet.") and not engine.started
    code = first.split('confirm: "')[1].split('"')[0]
    _call(tools["costly_audit"], {"confirm": code})
    assert engine.started[-1][1] == "costly_audit"


def test_scheduling_from_chat_and_listing(hub, engine):
    tools = _tools(hub)
    proposal = _call(tools["schedule_workflow"], {"workflow": "program_report",
                                                  "inputs": {"program": "Bhava", "period": "month"},
                                                  "cadence": "every monday at 09:00"})
    assert "Not saved yet. Mondays at 09:00 (Asia/Kolkata)" in proposal
    code = proposal.split('confirm: "')[1].split('"')[0]
    saved = _call(tools["schedule_workflow"], {"confirm": code})
    assert saved.startswith('Saved "Program report: Bhava, month". The first run worked.')
    table = _call(tools["my_schedules"], {})
    assert "| Program report: Bhava, month (Mondays at 09:00, Asia/Kolkata) |" in table
    phone = _call(tools["my_schedules"], {}, surface="telegram")
    assert "|" not in phone and phone.startswith("You have 1 schedule:")
    paused = _call(tools["change_schedule"], {"schedule": "Program report: Bhava, month", "action": "pause"})
    assert paused.startswith('Paused "Program report: Bhava, month"')
    reports = _call(tools["my_reports"], {})
    assert reports.startswith("**Yours**") and "/by/" in reports
    assert _call(tools["my_schedules"], {}, who=RAVI).startswith("You have no schedules")


def test_confirmation_recovery_without_tool_history(hub, engine, monkeypatch):
    from hubzoid._request_ctx import chat_scope

    tools = _tools(hub)
    store_for(hub).grant(MEERA, "irs", "wf_costly_audit", actor="t")
    with chat_scope("confirmation"):
        _call(tools["schedule_workflow"], {"workflow": "program_report",
              "inputs": {"program": "Bhava", "token": "hidden-value"}, "cadence": "daily at 09:00"})
        _call(tools["costly_audit"], {})
        assert "Pending proposals" not in _call(tools["my_schedules"], {})
    with chat_scope("confirmation"):
        schedules = _call(tools["my_schedules"], {})
        reports = _call(tools["my_reports"], {})
        assert "Pending proposals" in schedules and "schedule_workflow" in schedules
        assert "hidden-value" not in schedules and "••••" in schedules
        assert "costly_audit" in reports and "schedule_workflow" not in reports
        assert "Pending proposals" not in _call(tools["my_schedules"], {}, who=RAVI)
        # Account replacement cannot recover the old person's pending actions.
        store_for(hub).upsert_identity(email=MEERA, owui_id="replacement")
        assert personal.pending(hub, "irs", MEERA, kinds=("create", "run")) == []
        store_for(hub).upsert_identity(email=MEERA, owui_id="id-" + MEERA)
        now = time.time()
        monkeypatch.setattr(personal.time, "time", lambda: now + personal.PROPOSAL_SECONDS + 1)
        assert personal.pending(hub, "irs", MEERA, kinds=("create", "run")) == []


def test_change_and_stop_recovery_names_the_schedule(hub, engine):
    from hubzoid._request_ctx import chat_scope

    _, saved = _save(hub)
    schedule = saved["schedule"]
    tools = _tools(hub)
    with chat_scope("edit"):
        change = personal.propose_change(hub, "irs", MEERA, schedule.id,
                                        cadence="daily at 09:00", timezone_name=None)
        stop = personal.propose_stop(hub, "irs", MEERA, schedule.id)
    with chat_scope("edit"):
        result = _call(tools["my_schedules"], {})
        assert change["code"] in result and stop["code"] in result
        assert f'"schedule": "{schedule.name}"' in result
        assert '"tool": "change_schedule"' in result


# ---- delivery ------------------------------------------------------------------------------


def test_list_email_goes_one_by_one_to_accounts_and_company_addresses(hub, monkeypatch, tmp_path):
    from hubzoid import email_delivery
    from hubzoid.workflows.identity import RunIdentity

    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    (hub / "workflows" / "settings.yaml").write_text(
        "email_lists:\n  leads: [meera@co.org, new@co.org, eve@gmail.com]\n")
    ident = RunIdentity(TEAM_ACCT, "id-" + TEAM_ACCT, "run_as", TEAM_ACCT)
    out = email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="leads",
                                      subject="Weekly", body="# Numbers are in", body_format="markdown")
    from email import policy
    from email.parser import BytesParser
    from pathlib import Path
    preview = next(r for r in out["results"] if r["status"] == "previewed")
    message = BytesParser(policy=policy.default).parsebytes(Path(preview["outbox"]).read_bytes())
    assert "<h1" in message.get_body(preferencelist=("html",)).get_content()
    assert "To stop, ask" in message.get_body(preferencelist=("plain",)).get_content()
    by = {r["recipient"]: r["status"] for r in out["results"]}
    assert by == {"eve@gmail.com": "refused", "meera@co.org": "previewed", "new@co.org": "refused"}
    monkeypatch.setenv("HUBZOID_SHARE_DOMAINS", "co.org")
    for k, v in {"GOOGLE_CLIENT_ID": "i", "GOOGLE_CLIENT_SECRET": "s", "ENABLE_OAUTH_SIGNUP": "true",
                 "OAUTH_ALLOWED_DOMAINS": "co.org"}.items():
        monkeypatch.setenv(k, v)
    out = email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="leads",
                                      subject="Weekly", body="# Numbers are in", body_format="markdown")
    assert {r["recipient"]: r["status"] for r in out["results"]}["new@co.org"] == "previewed"
    assert email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="nope", subject="x",
                                       body="")["status"] == "refused"
    assert email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="leads", subject="x",
                                       body="y" * 2001)["status"] == "refused"

    from hubzoid import hub as run
    from hubzoid.workflows.testing import run_local

    def leads_note():
        assert run.setting("email_lists")["leads"]   # run_local reads workflows/settings.yaml
        return run.send_email("Weekly", email_list="leads", raise_on_failure=False)["status"]
    assert run_local(leads_note, hub_dir=hub, trigger="schedule").result == "previewed"
    assert run_local(leads_note, hub_dir=hub, as_user=MEERA).result == "refused"   # not from a personal run


def test_github_webhooks_check_the_body_signature(monkeypatch):
    from hubzoid.workflows import webhooks

    monkeypatch.setenv("WEBHOOK_SECRET_GITHUB", "s3cret")
    body = b'{"action":"opened"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    spec = {"verify": "github"}
    assert webhooks.verify(body, {"x-hub-signature-256": good}, "github", spec)
    assert not webhooks.verify(body + b" ", {"x-hub-signature-256": good}, "github", spec)
    assert not webhooks.verify(body, {}, "github", spec)


def test_run_errors_survive_the_run_history_and_retries():
    """DBOS pickles a failed step's error, and wraps a retried one."""
    import pickle

    from dbos._error import DBOSMaxStepRetriesExceeded

    from hubzoid.email_delivery import EmailError
    from hubzoid.structured import ModelOutputError

    for err in (ModelOutputError("bad JSON", "{x: high}"), arts.ArtifactError(409, "taken"),
                EmailError({"status": "failed", "message": "no"})):
        back = pickle.loads(pickle.dumps(err))
        assert type(back) is type(err) and vars(back) == vars(err) and str(back) == str(err)

    def step(*_args):
        raise DBOSMaxStepRetriesExceeded("hz_llm", 2, [TimeoutError(), ModelOutputError("bad", "raw")])
    with pytest.raises(ModelOutputError) as caught:
        runtime._last_error(step)({}, "hub", "me")
    assert caught.value.raw == "raw"


# ---- final review: accounts, run now, stop, names --------------------------------------------


def test_a_replaced_account_sees_none_of_the_old_schedules_or_reports(hub, engine):
    from hubzoid.tools import report_tools

    _, saved = _save(hub)
    assert personal.for_owner(hub, "irs", MEERA)                    # hers, saved as id-meera
    src = hub / "mine.html"
    src.write_text("mine")
    made = arts.publish(hub, hub="irs", owner=MEERA, owner_account="id-" + MEERA, source=src, title="Bhava",
                        workflow="program_report", run_id="r1", key="program-mine", series={"scope": "personal"})
    assert report_tools._reports_for(hub, MEERA)[0]
    store_for(hub).upsert_identity(email=MEERA, owui_id="id-new")   # a new account took the email
    assert personal.for_owner(hub, "irs", MEERA) == []
    assert not [r for r in report_tools._reports_for(hub, MEERA)[0] if "program-mine" in r[1]]
    # Publishing again as the new account starts fresh; the old company link doesn't follow.
    from hubzoid.artifacts import series

    series.domain_on(hub, series.get(hub, made["series_id"]), MEERA)
    src.write_text("theirs")
    again = arts.publish(hub, hub="irs", owner=MEERA, owner_account="id-new", source=src, title="Bhava",
                         workflow="program_report", run_id="r2", key="program-mine", series={"scope": "personal"})
    assert again["series_id"] == made["series_id"] and not series.domain_token(hub, series.get(hub, made["series_id"]))


def test_run_now_twice_in_five_minutes_says_so_and_stopping_retires_the_report(hub, engine):
    from hubzoid.artifacts import series

    _, saved = _save(hub)
    sid = saved["schedule"].id
    personal.run_now(hub, "irs", MEERA, sid)                         # finishes at once here
    started = len(engine.started)
    with pytest.raises(personal.ScheduleError, match="ran a few minutes ago"):
        personal.run_now(hub, "irs", MEERA, sid)                     # nothing would run: said, not hidden
    assert len(engine.started) == started
    (made,) = [x for x in series.for_owner(hub, "irs", MEERA) if x.schedule_id == sid]
    assert series.state(hub, made)["state"] == "live"
    personal.owner_stop(hub, "irs", MEERA, sid)
    assert series.state(hub, series.get(hub, made.id))["state"] == "stopped"   # "No longer updating"


def test_a_workflow_never_takes_a_core_tool_name(hub, engine):
    (hub / "workflows" / "clash").mkdir()
    (hub / "workflows" / "clash" / "clash.py").write_text(
        "from hubzoid import workflow\n@workflow(as_tool=True)\ndef my_reports():\n    return {}\n")
    names = [t.name for t in __import__("hubzoid.tools.report_tools", fromlist=["x"]).make(SimpleNamespace(hub_dir=hub))]
    assert names.count("my_reports") == 1 and "program_report" in names


def test_update_now_runs_for_the_account_the_copy_was_made_for(hub, engine, monkeypatch):
    from hubzoid.artifacts import series, web

    src = hub / "u.html"
    src.write_text("u")
    made = arts.publish(hub, hub="irs", owner=MEERA, owner_account="id-" + MEERA, source=src, title="Bhava",
                        workflow="program_report", run_id="r1", key="program-u", series={"scope": "personal"})
    monkeypatch.setattr(runtime, "_HUB_DIR", hub)
    web._start_update(hub, series.get(hub, made["series_id"]), MEERA)
    env = engine.started[-1][2]
    assert (env["acts_as"], env["acts_as_account"], env["trigger"]) == (MEERA, "id-" + MEERA, "update")


# 1.3.1 regressions from the release review.

def test_confirm_only_with_required_inputs(hub, engine):
    class Required(BaseModel):
        program: str
    wf = runtime._REGISTRY['costly_audit']
    runtime._REGISTRY['costly_audit'] = replace(wf, inputs=Required)
    from hubzoid.tools.report_tools import _run_tool
    tool = _run_tool(hub, runtime._REGISTRY['costly_audit'])
    store_for(hub).grant(MEERA, 'irs', 'wf_costly_audit', actor='t')
    first = _call(tool, {'program': 'Bhava'})
    code = first.split('confirm: "')[1].split('"')[0]
    _call(tool, {'confirm': code})
    assert 'required' not in tool.params_json_schema
    assert engine.started[-1][2]['inputs'] == {'program': 'Bhava'}
    assert engine.started, 'Following the tool instruction (only confirm) did not start the approved run'


def test_stopping_during_run_leaves_report_stopped(hub, engine, tmp_path):
    from hubzoid.workflows import personal
    from hubzoid.artifacts import series
    proposal, out = _save(hub)
    sid = out['schedule'].id
    run_id = out['run_id']
    found = series.for_owner(hub, 'irs', MEERA)[0]
    personal.owner_stop(hub, 'irs', MEERA, sid)
    assert series.state(hub, series.get(hub, found.id))['state'] == 'stopped'
    src = tmp_path / 'late.html'
    src.write_text('finished after stop')
    arts.publish(hub, hub='irs', owner=MEERA, owner_account='id-'+MEERA, source=src,
                 workflow='program_report', run_id='already-running', key=found.key,
                 series={'scope':'personal','schedule_id':sid})
    series.finish_run(hub, 'irs', 'program_report', 'already-running',
                      trigger='personal_schedule', scope='personal', schedule_id=sid, ok=True)
    assert series.state(hub, series.get(hub, found.id))['state'] == 'stopped'


def test_queue_failure_counts_towards_pause(hub, engine, monkeypatch):
    from hubzoid.workflows import personal
    proposal, out = _save(hub)
    sid = out['schedule'].id
    run_id = out['run_id']
    personal.after_run(hub, 'irs', {'schedule_id':sid}, run_id, ok=True, cause=None, published=[])
    def fail(*args, **kwargs):
        raise RuntimeError('queue unavailable')
    monkeypatch.setattr(runtime, 'start_run', fail)
    for i in range(3):
        s = personal.get(hub, sid)
        personal.fire_due(hub, 'irs', now=s.next_fire + 1)
    s = personal.get(hub, sid)
    assert s.state == 'paused'


def test_run_for_me_returns_unkeyed_artifact(hub, engine, monkeypatch):
    from types import SimpleNamespace
    published=[]
    def start(name, envelope, *, workflow_id=None):
        src=hub/'unkeyed.html'
        src.write_text('one-off report')
        published.append(arts.publish(hub, hub='irs', owner=MEERA, owner_account='id-'+MEERA,
            source=src, workflow=name, run_id=workflow_id))
        engine.status[workflow_id]='SUCCESS'
        return SimpleNamespace(get_workflow_id=lambda:workflow_id)
    monkeypatch.setattr(runtime, 'start_run', start)
    answer=_call(_tools(hub)['program_report'], {'program':'Bhava'})
    assert published[0]['url'] in answer, 'Published artifact is absent from chat'


def test_dispatch_intent_recovers_same_run_after_interruption(hub, engine, monkeypatch):
    _, saved = _save(hub)
    sid = saved["schedule"].id
    personal.after_run(hub, "irs", {"schedule_id": sid}, saved["run_id"], ok=True,
                       cause=None, published=[])
    original = runtime.start_run

    class Interrupted(BaseException):
        pass

    def crash(*args, **kwargs):
        original(*args, **kwargs)  # admitted, but the dispatcher never acknowledged it
        raise Interrupted()

    monkeypatch.setattr(runtime, "start_run", crash)
    now = personal.get(hub, sid).next_fire + 1
    with pytest.raises(Interrupted):
        personal.fire_due(hub, "irs", now=now)
    intent = personal.get(hub, sid)
    assert intent.last_status == "dispatching" and intent.next_fire > now
    admitted = len(engine.started)
    monkeypatch.setattr(runtime, "start_run", original)
    assert personal.fire_due(hub, "irs", now=now + 1) == [intent.last_run]
    assert len(engine.started) == admitted  # same deterministic DBOS id
    assert personal.get(hub, sid).last_status == "running"


def test_edit_during_dispatch_never_queues_the_old_inputs(hub, engine, monkeypatch):
    _, saved = _save(hub)
    s = personal.get(hub, saved["schedule"].id)
    personal.after_run(hub, "irs", {"schedule_id": s.id}, saved["run_id"],
                       ok=True, cause=None, published=[])
    original = personal.problem

    def edit_after_read(hub_dir, hub_name, snapshot):
        result = original(hub_dir, hub_name, snapshot)
        personal.edit(hub_dir, hub_name, MEERA, snapshot.id,
                      inputs={"program": "Changed"}, cadence=snapshot.cadence,
                      timezone_name=snapshot.timezone, revision=snapshot.updated)
        return result

    monkeypatch.setattr(personal, "problem", edit_after_read)
    before = len(engine.started)
    assert personal.fire_due(hub, "irs", now=s.next_fire + 1) == []
    assert len(engine.started) == before
    assert personal.get(hub, s.id).values["program"] == "Changed"


def test_old_completion_preserves_new_dispatch_and_admin_pause(hub, engine):
    _, saved = _save(hub)
    sid = saved["schedule"].id
    personal._update(hub, sid, last_run="new-run", last_status="dispatching")
    personal.after_run(hub, "irs", {"schedule_id": sid}, saved["run_id"], ok=True,
                       cause=None, published=[])
    assert personal.get(hub, sid).last_run == "new-run"
    personal.set_paused(hub, "irs", "admin@co.org", sid, True, admin=True)
    personal._update(hub, sid, failures=2)
    personal.after_run(hub, "irs", {"schedule_id": sid}, "new-run", ok=False,
                       cause="timeout", published=[])
    current = personal.get(hub, sid)
    assert (current.state, current.reason, current.failures) == ("paused", "admin", 3)


def test_edit_replaces_inputs_preserves_pause_and_rejects_stale_save(hub, engine):
    _, saved = _save(hub)
    sid = saved["schedule"].id
    s = personal.set_paused(hub, "irs", "admin@co.org", sid, True, admin=True)
    edited = personal.edit(hub, "irs", MEERA, sid, inputs={"program": "New program", "period": "month"},
                           cadence="daily at 18:00", timezone_name="UTC", revision=s.updated)
    assert edited.values["program"] == "New program" and edited.values["period"] == "month"
    assert (edited.state, edited.reason, edited.cadence) == ("paused", "admin", "daily at 18:00")
    with pytest.raises(personal.ScheduleError, match="changed while"):
        personal.edit(hub, "irs", MEERA, sid, inputs={}, cadence="daily at 09:00",
                      timezone_name="UTC", revision=s.updated)
    with pytest.raises(personal.ScheduleError, match="no schedule"):
        personal.edit(hub, "irs", RAVI, sid, inputs={}, cadence="daily at 09:00",
                      timezone_name="UTC", revision=edited.updated)


def test_change_tool_confirms_saved_input_values(hub, engine):
    _, saved = _save(hub)
    tool = _tools(hub)["change_schedule"]
    first = _call(tool, {"schedule": saved["schedule"].id, "action": "change",
                         "inputs": {"program": "New program", "period": "month"}})
    code = first.split('confirm: "')[1].split('"')[0]
    _call(tool, {"confirm": code})
    assert personal.get(hub, saved["schedule"].id).values["program"] == "New program"


def test_confirmation_is_bound_to_account_and_workflow(hub, engine):
    code = personal.propose(hub, "irs", MEERA, "run", {"workflow": "program_report", "inputs": {}})
    with pytest.raises(personal.ScheduleError, match="doesn't match this report"):
        personal.take(hub, "irs", MEERA, code, kinds=("run",), workflow="costly_audit")
    store_for(hub).upsert_identity(email=MEERA, owui_id="replacement")
    with pytest.raises(personal.ScheduleError, match="different account"):
        personal.take(hub, "irs", MEERA, code, kinds=("run",), workflow="program_report")


def test_personal_results_require_admission_and_current_account_identity(hub, monkeypatch):
    import dbos
    from hubzoid.workflows import observe

    monkeypatch.setattr(observe, "_missing_sqlite", lambda _: False)
    env = {"trigger": "tool", "scope": "personal", "acts_as": MEERA, "acts_as_account": "id-" + MEERA}
    prefix = personal.tool_run_prefix("program_report", MEERA, "id-" + MEERA)
    run = SimpleNamespace(workflow_id=prefix + "input-deadbeef", name="program_report",
                          input={"args": ("irs", None, env), "kwargs": {}},
                          status="SUCCESS", output={"answer": "Private result"}, created_at=1000)
    who = {"subject": MEERA, "account_id": "id-" + MEERA, "source": "caller"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def list_workflows(self, **kwargs):
            assert kwargs["application_name"] == runtime._app_name("irs")
            prefixes = kwargs["workflow_id_prefix"]
            return [run] if run.workflow_id.startswith(tuple(prefixes) if isinstance(prefixes, list) else prefixes) else []

        def list_workflow_steps(self, _):
            return [{"function_name": "hz_run_identity", "output": who}]

        def destroy(self):
            pass

    monkeypatch.setattr(dbos, "DBOSClient", Client)
    own = observe.personal_runs(hub, MEERA, "id-" + MEERA)
    assert own[0]["answer"] == "Private result" and "input" not in own[0]
    assert observe.personal_runs(hub, RAVI, "id-" + RAVI) == []
    assert observe.personal_runs(hub, RAVI, "id-" + RAVI, run_id=run.workflow_id) == []
    assert observe.personal_runs(hub, MEERA, "replacement") == []
    who["account_id"] = "other-account"
    assert observe.personal_runs(hub, MEERA, "id-" + MEERA) == []
    who.clear()
    run.status, run.output = "ENQUEUED", None
    assert observe.personal_runs(hub, MEERA, "id-" + MEERA)[0]["answer"] is None


def test_my_reports_keeps_saved_links_when_history_is_unavailable(hub, engine, monkeypatch):
    from hubzoid.workflows import observe

    _call(_tools(hub)["program_report"], {"program": "Bhava"})

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(observe, "personal_runs", unavailable)
    answer = _call(_tools(hub)["my_reports"], {})
    assert "temporarily unavailable" in answer and "Bhava report" in answer


def test_run_history_also_binds_results_to_the_recorded_account():
    from hubzoid.workflows.observe import may_see_results

    owner = {"source": "caller", "subject": MEERA, "account_id": "original"}
    assert may_see_results(owner, MEERA, account="original")
    assert not may_see_results(owner, MEERA, account="replacement")
    assert not may_see_results(owner, MEERA)
    assert not may_see_results(owner, RAVI, account="original")


def test_personal_history_limits_after_account_filter_and_merges_workflows(hub, monkeypatch):
    import dbos
    from hubzoid.workflows import observe

    monkeypatch.setattr(observe, "_missing_sqlite", lambda _: False)
    owner = {"subject": MEERA, "account_id": "id-" + MEERA, "source": "caller"}
    env = {"trigger": "tool", "scope": "personal", "acts_as": MEERA,
           "acts_as_account": "id-" + MEERA}
    def run(name, actor, number):
        prefix = personal.tool_run_prefix(name, actor, "id-" + actor)
        return SimpleNamespace(workflow_id=prefix + f"input-{number}", name=name,
            input={"args": ("irs", None, env)}, status="SUCCESS",
            output={"answer": str(number)}, created_at=number)

    # A busy teammate would consume the old hub-wide limit before our first result.
    own = [run("program_report", MEERA, n) for n in range(55)]
    own += [run("costly_audit", MEERA, 60)]
    all_runs = own + [run("program_report", RAVI, n) for n in range(100, 160)]
    class Client:
        def __init__(self, **kwargs): pass
        def destroy(self): pass
        def list_workflow_steps(self, _):
            return [{"function_name": "hz_run_identity", "output": owner}]
        def list_workflows(self, **kwargs):
            prefixes = kwargs["workflow_id_prefix"]
            prefixes = tuple(prefixes) if isinstance(prefixes, list) else prefixes
            selected = [w for w in all_runs if w.workflow_id.startswith(prefixes)]
            if kwargs.get("workflow_ids"):
                selected = [w for w in selected if w.workflow_id in kwargs["workflow_ids"]]
            selected = sorted(selected, key=lambda w: w.created_at, reverse=True)[:kwargs["limit"]]
            assert len(selected) <= 50  # Payload loading stays bounded across workflows.
            return selected

    monkeypatch.setattr(dbos, "DBOSClient", Client)
    result = observe.personal_runs(hub, MEERA, "id-" + MEERA)
    assert len(result) == 50
    assert [r["started"] for r in result] == [60, *range(54, 5, -1)]
    # Existing IDs are unchanged, and exact lookup survives definition removal.
    old_key = hashlib.sha256(json.dumps([MEERA, "id-" + MEERA]).encode()).hexdigest()[:10]
    assert own[0].workflow_id.startswith(f"tool-program_report-{old_key}-")
    monkeypatch.setattr(runtime, "load_definitions", lambda _: [])
    assert observe.personal_runs(hub, MEERA, "id-" + MEERA, run_id=own[0].workflow_id)[0]["answer"] == "0"


def test_repeat_a_request_is_a_personal_workflow_with_its_own_access(hub, engine):
    """"Repeat this every Monday" saves the built-in repeated_request workflow.
    Everyone who uses the agent may run and schedule their own out of the box;
    it is never offered as a "run for me" tool of its own."""
    from hubzoid.workflows import access as wf_access

    tools = _tools(hub)
    assert "repeat_request" in tools and "repeated_request" not in tools
    ask = {"prompt": "Summarise low stock items", "cadence": "every monday at 09:00", "timezone": "Europe/London"}
    d = wf_access.authorize(hub, RAVI, "repeated_request", "schedule")
    assert d.allowed and "everyone who uses this agent" in d.sources[0]
    assert not wf_access.authorize(hub, "stranger@co.org", "repeated_request", "run").allowed
    proposal = _call(tools["repeat_request"], ask)
    assert proposal.startswith("Not saved yet. Mondays at 09:00 (Europe/London)")
    assert 'exactly: "Summarise low stock items"' in proposal and not engine.started
    code = proposal.split('confirm: "')[1].split('"')[0]
    saved = _call(tools["repeat_request"], {"confirm": code})
    assert saved.startswith('Saved "') and "new private chat" in saved
    wid, name, env = engine.started[-1]
    assert name == "repeated_request" and env["acts_as"] == MEERA
    assert env["inputs"] == {"prompt": "Summarise low stock items"}
    assert "repeated_request" in {r["key"] for r in wf_access.resources(hub)}
