"""Personal ("Run for me") schedules (hubzoid.workflows.personal): the email
the owner gets for each event, nothing when the hub has notices off, a failed
send that never breaks the schedule, the words a schedule's state and last
run show, and an access check that fails closed."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hubzoid.workflows import personal, runtime


def schedule(**kw):
    base = dict(id="ps-1", hub="reports", owner="ana@example.org", owner_account="acc-1", name="Weekly sales",
                workflow="sales_report", values={}, timezone="Asia/Kolkata", last_run="run-7", updated=1700000000.0,
                last_cause=None, reason=None, state="active", last_status=None, last_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def sent(monkeypatch, tmp_path):
    from hubzoid import email_delivery

    out = []
    monkeypatch.setattr(personal, "email_on", lambda hub_dir: True)
    monkeypatch.setattr(personal, "settings", lambda hub_dir: {"pause_after_failures": 3})
    monkeypatch.setattr(personal, "cadence_words", lambda s: "every Monday at 09:00")
    monkeypatch.setattr(personal, "manage_url", lambda hub_dir, s: "https://hz.example/schedules/ps-1")
    monkeypatch.setattr(runtime, "definition", lambda hub_dir, name: SimpleNamespace(inputs=None))
    monkeypatch.setattr(runtime, "title_for", lambda name: "Sales report")
    monkeypatch.setattr(email_delivery, "send_to_owner", lambda hub_dir, **kw: out.append(kw))
    return out


@pytest.mark.parametrize("kind,s,subject,phrase", [
    ("created", schedule(), "New schedule: Weekly sales", "every Monday at 09:00 (Asia/Kolkata)"),
    ("changed", schedule(), "Weekly sales: new report", "ran and changed"),
    ("failed", schedule(last_cause="timeout"), "Weekly sales: run failed", "If the next 2 runs also fail"),
    ("paused", schedule(reason="access"), "Weekly sales: paused", "because your access changed"),
    ("paused", schedule(reason="odd"), "Weekly sales: paused", "because it can't run"),
    ("admin_paused", schedule(), "Weekly sales: paused by an admin", "Only they can resume it"),
    ("admin_stopped", schedule(), "Weekly sales: stopped by an admin", "Its live link keeps the last copy"),
])
def test_each_notice_has_its_subject_and_words(tmp_path, sent, kind, s, subject, phrase):
    personal.notify(tmp_path, "reports", s, kind, published=[])
    [mail] = sent
    assert mail["subject"] == subject and phrase in mail["body"]
    assert mail["identity"].subject == "ana@example.org" and mail["idem_key"].startswith(f"ps:ps-1:{kind}:run-7:")


def test_no_notice_when_off_unknown_or_without_a_schedule(tmp_path, sent, monkeypatch):
    personal.notify(tmp_path, "reports", None, "created")
    personal.notify(tmp_path, "reports", schedule(), "celebrated")
    monkeypatch.setattr(personal, "email_on", lambda hub_dir: False)
    personal.notify(tmp_path, "reports", schedule(), "created")
    assert sent == []


def test_a_failed_send_never_breaks_the_schedule(tmp_path, sent, monkeypatch, caplog):
    from hubzoid import email_delivery

    def boom(hub_dir, **kw):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(email_delivery, "send_to_owner", boom)
    with caplog.at_level(logging.ERROR):
        personal.notify(tmp_path, "reports", schedule(), "created")   # logged, not raised
    assert any("could not email" in r.getMessage() and r.exc_info for r in caplog.records)


@pytest.mark.parametrize("state,reason,words", [
    ("testing", None, "First run in progress"), ("active", None, "Active"), ("stopped", None, "Stopped"),
    ("paused", "owner", "Paused by owner"), ("paused", "removed", "Paused: the workflow was removed"),
    ("paused", "unknown", "Paused"),
])
def test_state_words(state, reason, words):
    assert personal.state_words(schedule(state=state, reason=reason)) == words


def test_last_run_words(monkeypatch):
    monkeypatch.setattr(personal, "_when", lambda ts, tz: "today at 09:00")
    assert personal.last_words(schedule(last_status="updated")) == "Updated today at 09:00"
    assert personal.last_words(schedule(last_status="no_change")) == "No change today at 09:00"
    assert personal.last_words(schedule(last_status="failed", last_cause="nonsense")).startswith("Failed today at 09:00: ")
    assert personal.last_words(schedule(last_status="running")) == "Running now"
    assert personal.last_words(schedule(last_status=None)) == "Not run yet"


def test_the_access_check_fails_closed(tmp_path, monkeypatch):
    from hubzoid import access

    def broken(hub_dir):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(access, "store_for", broken)
    assert personal._can(tmp_path, "ana@example.org", "reports", "workflows_run") is False


# ---- the gate before a schedule is saved (_validated) ---------------------------------

def gate(monkeypatch, *, wf=None, run=True, sched=True, mine=(), limits=None):
    from hubzoid.workflows import access as wf_access

    wf = wf if wf is not None else SimpleNamespace(as_tool=True, schedulable=True, title="Sales report",
                                                   inputs=None, min_interval=0)
    monkeypatch.setattr(personal, "_definition", lambda hub_dir, name: wf)
    monkeypatch.setattr(wf_access, "authorize", lambda hub_dir, actor, workflow, what: SimpleNamespace(
        allowed=run if what == "run" else sched))
    monkeypatch.setattr(personal, "settings", lambda hub_dir: limits or {"min_interval": 3600, "per_person": 5})
    monkeypatch.setattr(personal, "for_owner", lambda hub_dir, hub, actor: list(mine))


def save(tmp_path, cadence="daily at 09:00", tz="Asia/Kolkata", values=None):
    return personal._validated(tmp_path, "reports", "ana@example.org", "sales_report", values, cadence, tz)


def test_a_schedule_that_fits_is_accepted(tmp_path, monkeypatch):
    gate(monkeypatch)
    wf, data, cron = save(tmp_path)
    assert wf.title == "Sales report" and data == {} and cron == "0 9 * * *"


@pytest.mark.parametrize("setup,call,message", [
    ({"wf": SimpleNamespace(as_tool=False)}, {}, "There is no report named 'sales_report' here."),
    ({"wf": SimpleNamespace(as_tool=True, schedulable=False, title="Sales report")}, {}, "can't be scheduled"),
    ({"run": False}, {}, "You can't run Sales report yet."),
    ({"sched": False}, {}, "You can run this report but not schedule it."),
    ({}, {"cadence": "whenever you like"}, "as a schedule. Try"),
    ({}, {"tz": "Mars/Olympus"}, "isn't a time zone I know"),
    ({}, {"cadence": "every 5 minutes"}, "Personal schedules run at most once every hour."),
    ({"wf": SimpleNamespace(as_tool=True, schedulable=True, title="Sales report", inputs=None, min_interval=86400)},
     {"cadence": "every 2 hours"}, "Sales report can run at most once every day."),
])
def test_each_refusal_says_why(tmp_path, monkeypatch, setup, call, message):
    from hubzoid.workflows.personal import ScheduleError

    gate(monkeypatch, **setup)
    with pytest.raises(ScheduleError) as err:
        save(tmp_path, **call)
    assert message in str(err.value)


def test_limits_per_person_and_duplicates(tmp_path, monkeypatch):
    from hubzoid.workflows.personal import ScheduleError

    same = schedule(workflow="sales_report", values={}, timezone="Asia/Kolkata", cadence="daily at 09:00",
                    name="Morning sales")
    gate(monkeypatch, mine=[same])
    with pytest.raises(ScheduleError, match='You already have "Morning sales"'):
        save(tmp_path)
    gate(monkeypatch, mine=[same] * 5)
    with pytest.raises(ScheduleError, match="You have 5 schedules on this agent"):
        save(tmp_path, cadence="daily at 18:00")
