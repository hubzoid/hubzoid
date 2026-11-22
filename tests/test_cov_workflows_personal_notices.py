"""Personal ("Run for me") schedules (hubzoid.workflows.personal): the email
the owner gets for each event, nothing when the hub has notices off, a failed
send that never breaks the schedule, the words a schedule's state and last
run show, and an access check that fails closed."""
from __future__ import annotations

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


def test_a_failed_send_never_breaks_the_schedule(tmp_path, sent, monkeypatch):
    from hubzoid import email_delivery

    def boom(hub_dir, **kw):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(email_delivery, "send_to_owner", boom)
    personal.notify(tmp_path, "reports", schedule(), "created")       # logged, not raised


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
