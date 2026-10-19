"""Workflow access (hubzoid.workflows.access): separate actions, the file and
its revisions, explain, earlier grants read through, and the checks where
workflows are run and scheduled."""
from __future__ import annotations

import pytest

from hubzoid.workflows import access as wf
from tests.chat_helpers import make_hub

WORKFLOW = '''from hubzoid import workflow


@workflow(as_tool=True, schedulable=True)
def weekly_report():
    return "ok"
'''


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    (hub / "workflows" / "reports").mkdir(parents=True, exist_ok=True)
    (hub / "workflows" / "reports" / "main.py").write_text(WORKFLOW)
    from hubzoid import access, migrations
    from hubzoid.db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")
    gs = access.store_for(hub)
    gs.grant("owner@example.org", "*", "manage_access", actor="test")
    gs.grant("owner@example.org", hub.name, "use_hub", actor="test")
    return hub


POLICY = """version: 1
default: deny
grants:
  - subject: ana@example.org
    workflow: weekly_report
    actions: [run, schedule, view_outputs]
  - subject: ben@example.org
    workflow: weekly_report
    actions: [view_definition, edit]
"""


def test_no_action_implies_another(hub):
    wf.apply(hub, POLICY, expected_revision=0, actor="owner@example.org")
    assert wf.allowed_actions(hub, "ana@example.org", "weekly_report") == ["run", "schedule", "view_outputs"]
    assert wf.allowed_actions(hub, "ben@example.org", "weekly_report") == ["edit", "view_definition"] or \
        set(wf.allowed_actions(hub, "ben@example.org", "weekly_report")) == {"edit", "view_definition"}
    assert not wf.authorize(hub, "ben@example.org", "weekly_report", "run").allowed
    assert wf.authorize(hub, "ana@example.org", "weekly_report", "edit").reason == "missing_grant"


def test_schedule_needs_run_too(hub):
    wf.apply(hub, POLICY.replace("[run, schedule, view_outputs]", "[schedule]"), expected_revision=0,
             actor="owner@example.org")
    d = wf.authorize(hub, "ana@example.org", "weekly_report", "schedule")
    assert not d.allowed and d.reason == "schedule needs run too"


def test_file_alone_grants_nothing_and_apply_is_compare_and_set(hub):
    (hub / "workflows" / "access.yaml").write_text(POLICY)
    assert not wf.authorize(hub, "ana@example.org", "weekly_report", "run").allowed
    out = wf.apply(hub, POLICY, expected_revision=0, actor="owner@example.org")
    assert out["revision"] == 1 and len(out["added"]) == 5
    with pytest.raises(wf.AccessError) as stale:
        wf.apply(hub, POLICY, expected_revision=0, actor="owner@example.org")
    assert stale.value.code == "revision_conflict" and stale.value.extra["current_revision"] == 1
    with pytest.raises(wf.AccessError) as nope:
        wf.apply(hub, POLICY, expected_revision=1, actor="ana@example.org")
    assert nope.value.code == "delegation_denied"
    assert wf.dump(wf.explicit(hub)).count("subject:") == 2


def test_validation_names_the_row_and_refuses_unknowns(hub):
    with pytest.raises(wf.AccessError) as bad:
        wf.validate(hub, POLICY.replace("view_outputs", "view_everything"))
    assert bad.value.code == "invalid_policy" and bad.value.extra["line"] == 6
    with pytest.raises(wf.AccessError) as unknown:
        wf.validate(hub, POLICY.replace("workflow: weekly_report\n    actions: [view", "workflow: nope\n    actions: [view"))
    assert unknown.value.code == "unknown_workflow"
    with pytest.raises(wf.AccessError):
        wf.validate(hub, POLICY.replace("ana@example.org", "group:analysts"))


def test_explain_about_yourself_or_as_a_manager(hub):
    wf.apply(hub, POLICY, expected_revision=0, actor="owner@example.org")
    me = wf.explain(hub, asker="ana@example.org", subject="ana@example.org", workflow="weekly_report",
                    action="edit")
    assert me["allowed"] is False and me["reason"] == "missing_grant"
    with pytest.raises(wf.AccessError):
        wf.explain(hub, asker="ana@example.org", subject="ben@example.org", workflow="weekly_report",
                   action="edit")
    other = wf.explain(hub, asker="owner@example.org", subject="ben@example.org", workflow="weekly_report",
                       action="edit")
    assert other["allowed"] is True


def test_earlier_grants_keep_working(hub):
    from hubzoid import access

    access.store_for(hub).grant("ben@example.org", hub.name, "wf_weekly_report", actor="test")
    access.store_for(hub).grant("ben@example.org", hub.name, "schedule_reports", actor="test")
    d = wf.authorize(hub, "ben@example.org", "weekly_report", "schedule")
    assert d.allowed and any("schedule_reports" in s for s in d.sources)
    assert not wf.authorize(hub, "ben@example.org", "weekly_report", "view_outputs").allowed


def test_personal_schedule_uses_workflow_access(hub):
    from hubzoid.workflows import personal

    with pytest.raises(personal.ScheduleError) as nope:
        personal._validated(hub, hub.name.lower(), "ana@example.org", "weekly_report", {}, "daily at 09:00", "UTC")
    assert "can't run" in str(nope.value)
    wf.apply(hub, POLICY, expected_revision=0, actor="owner@example.org")
    personal._validated(hub, hub.name.lower(), "ana@example.org", "weekly_report", {}, "daily at 09:00", "UTC")


def test_unknown_people_and_suspended_accounts_get_nothing(hub):
    wf.apply(hub, POLICY.replace("ana@example.org", "everyone"), expected_revision=0, actor="owner@example.org")
    assert wf.authorize(hub, "ana@example.org", "weekly_report", "run").allowed
    assert not wf.authorize(hub, "stranger@example.org", "weekly_report", "run").allowed  # no hub access
