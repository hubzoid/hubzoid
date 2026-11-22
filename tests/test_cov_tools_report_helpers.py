"""The report tools' shared pieces (hubzoid.tools.report_tools): who may use
them (signed in, on a chat surface; Slack is told to use the web chat), when
a run happened in the person's time zone, links as text on WhatsApp and
Telegram, a run's status and failure cause when the engine can't answer,
waiting for a run that stops to ask a person, and the tools are simply absent
when the workflow definitions can't be read. The engine is a fake."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from hubzoid import _request_ctx
from hubzoid.access import Identity, identity_scope
from hubzoid.tools import report_tools as rt
from hubzoid.workflows import outcomes, runtime


def test_who_may_use_reports():
    with identity_scope(Identity()):
        assert rt._caller() == (None, "system", "Sign in to use reports.")
    with identity_scope(Identity.make("ana@x.org", surface="slack-dm")):
        assert "aren't available in Slack" in rt._caller()[2]
    with identity_scope(Identity.make("workflow:daily", surface="owui")):
        assert rt._caller()[2] == "Reports aren't available here. Use the web chat."
    with identity_scope(Identity.make("Ana@X.org", surface="owui")):
        assert rt._caller() == ("ana@x.org", "owui", "")


def test_when_and_links():
    assert rt._ago(None) == "not yet"
    now = time.time()
    assert rt._ago(now, "Asia/Kolkata").startswith("today ")
    assert rt._ago(now - 86400, "No/Such_Zone").startswith("yesterday ")       # an unknown zone reads as UTC
    assert rt._ago(now - 10 * 86400)[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    assert rt._link("Sales", "https://h/x", "whatsapp") == "Sales: https://h/x"
    assert rt._link("Sales", "https://h/x", "owui") == "[Sales](https://h/x)"


def test_status_and_cause_when_the_engine_cant_answer(monkeypatch):
    def gone(run_id):
        raise RuntimeError("no such run")

    monkeypatch.setattr(runtime, "_DBOS", SimpleNamespace(get_workflow_status=gone,
                                                          retrieve_workflow=lambda run_id: SimpleNamespace(
                                                              get_result=lambda: "fine")), raising=False)
    assert rt._status("r-1") is None
    assert rt._failure_cause("r-1") == outcomes.CAUSES["error"]           # it returned: an unspecified error

    def raises():
        raise TimeoutError("model slow")

    monkeypatch.setattr(runtime, "_DBOS", SimpleNamespace(
        get_workflow_status=lambda run_id: None,
        retrieve_workflow=lambda run_id: SimpleNamespace(get_result=raises)), raising=False)
    assert rt._status("r-1") is None
    assert rt._failure_cause("r-1") == outcomes.CAUSES[outcomes.cause_of(TimeoutError("model slow"))]


def test_waiting_ends_when_the_run_asks_a_person(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "_status", lambda run_id: "PENDING")
    monkeypatch.setattr(rt, "_asking", lambda hub_dir, run_id: {"id": "req-1"})
    assert asyncio.run(rt._wait("r-1", 5, tmp_path)) == "ASKING"
    monkeypatch.setattr(rt, "_status", lambda run_id: "SUCCESS")
    assert asyncio.run(rt._wait("r-1", 5)) == "SUCCESS"
    assert asyncio.run(rt._wait("r-1", 0)) is None                      # no time to wait: no status


def test_the_person_time_zone_falls_back_to_the_browser(tmp_path, monkeypatch):
    from hubzoid.chat import config as chat_config

    def unreadable(hub_dir):
        raise RuntimeError("preferences unreadable")

    monkeypatch.setattr(chat_config, "service_for", unreadable)
    monkeypatch.setattr(_request_ctx, "browser_zone", lambda account: "Europe/Lisbon" if account == "ana@x.org" else None)
    assert rt._person_timezone(tmp_path, "Ana@X.org") == "Europe/Lisbon"
    monkeypatch.setattr(_request_ctx, "browser_zone", lambda account: None)
    assert rt._person_timezone(tmp_path, "ana@x.org") is None


def test_no_tools_when_definitions_cant_be_read(tmp_path, monkeypatch):
    from hubzoid.workflows import boot

    monkeypatch.setattr(boot, "schedules_enabled", lambda: True)
    monkeypatch.setattr(rt, "switched_off", lambda cap: False)

    def unreadable(hub_dir):
        raise SyntaxError("workflows/daily.py line 3")

    monkeypatch.setattr(runtime, "load_definitions", unreadable)
    assert rt.make(SimpleNamespace(hub_dir=tmp_path)) == []
    monkeypatch.setattr(runtime, "load_definitions", lambda hub_dir: [SimpleNamespace(as_tool=False)])
    assert rt.make(SimpleNamespace(hub_dir=tmp_path)) == []
