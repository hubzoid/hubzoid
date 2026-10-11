"""Workflows from chat: typed inputs, Run for me, personal schedules and their
chat tools, list email and GitHub signatures. No model and no DBOS engine:
the engine is a small fake that records what would be queued."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
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
        with pytest.raises(personal.ScheduleError, match="next message"):
            personal.confirm(hub, "irs", MEERA, proposal["code"])       # the model can't say yes
    assert proposal["text"].startswith("Not saved yet. Mondays at 09:00 (Asia/Kolkata)")
    assert personal.for_owner(hub, "irs", MEERA) == []                  # nothing stored yet
    with pytest.raises(personal.ScheduleError, match="doesn't match"):
        personal.confirm(hub, "irs", RAVI, proposal["code"])            # someone else's code
    with chat_scope("c1"):                                              # the person's yes
        assert personal.propose_create(hub, "irs", MEERA, **ask)["code"] == proposal["code"]
        saved = personal.confirm(hub, "irs", MEERA, proposal["code"])
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
    personal.after_run(hub, "irs", {"schedule_id": sid}, saved["run_id"], ok=True, cause=None, published=[])
    for i in range(3):
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


# ---- delivery ------------------------------------------------------------------------------


def test_list_email_goes_one_by_one_to_accounts_and_company_addresses(hub, monkeypatch, tmp_path):
    from hubzoid import email_delivery
    from hubzoid.workflows.identity import RunIdentity

    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    (hub / "workflows" / "settings.yaml").write_text(
        "email_lists:\n  leads: [meera@co.org, new@co.org, eve@gmail.com]\n")
    ident = RunIdentity(TEAM_ACCT, "id-" + TEAM_ACCT, "run_as", TEAM_ACCT)
    out = email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="leads",
                                      subject="Weekly", body="Numbers are in.")
    by = {r["recipient"]: r["status"] for r in out["results"]}
    assert by == {"eve@gmail.com": "refused", "meera@co.org": "previewed", "new@co.org": "refused"}
    monkeypatch.setenv("HUBZOID_SHARE_DOMAINS", "co.org")
    for k, v in {"GOOGLE_CLIENT_ID": "i", "GOOGLE_CLIENT_SECRET": "s", "ENABLE_OAUTH_SIGNUP": "true",
                 "OAUTH_ALLOWED_DOMAINS": "co.org"}.items():
        monkeypatch.setenv(k, v)
    out = email_delivery.send_to_list(hub, hub="irs", identity=ident, list_name="leads",
                                      subject="Weekly", body="Numbers are in.")
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
