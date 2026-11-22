"""Durable webhook admission (hubzoid.workflows.events): workflows/settings.yaml
rules, webhook names, admitting an event once (and refusing the same key with
other content), the backlog cap, failing and redriving, and what reconcile
does with each coordinator run it finds. DBOS and the engine's owner are fakes."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from hubzoid.workflows import events, runtime


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "WEBHOOK_INBOUND_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    h = tmp_path / "alpha"
    (h / "workflows").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: alpha\n---\nx")
    return h


def settings_file(hub, text):
    (hub / "workflows" / "settings.yaml").write_text(text)


@pytest.mark.parametrize("text,message", [
    ("- just a list\n", "must be a mapping"),
    ("max_executor_threads: 0\n", "max_executor_threads must be a positive integer"),
    ("max_executor_threads: true\n", "max_executor_threads must be a positive integer"),
    ("alerts: [x]\n", "alerts must be a mapping"),
    ("alerts: {to: [{pager: x}]}\n", "webhook, slack or email destinations"),
    ("alerts: {to: [{email: '  '}]}\n", "webhook, slack or email destinations"),
    ("alerts: {pause_after_failures: -1}\n", "pause_after_failures must be a nonnegative integer"),
    ("alerts: {failures_in_a_row: false}\n", "failures_in_a_row must be a nonnegative integer"),
    ("webhook_retry_delays: [1, -5]\n", "at most 9 nonnegative seconds"),
    ("webhook_retry_delays: [1,1,1,1,1,1,1,1,1,1]\n", "at most 9 nonnegative seconds"),
])
def test_settings_refuse_each_bad_value(hub, text, message):
    settings_file(hub, text)
    with pytest.raises(ValueError, match=message):
        events.settings(hub)


def test_settings_defaults_and_a_valid_file(hub):
    assert events.settings(hub) == {}
    settings_file(hub, "max_executor_threads: 8\nalerts:\n  to: [{slack: '#ops'}, {email: a@x.org}]\n"
                       "  cooldown: 30m\nwebhook_retry_delays: [0, 30.5]\n")
    assert events.settings(hub)["max_executor_threads"] == 8


@given(delays=st.lists(st.one_of(st.integers(-5, 10_000), st.floats(allow_nan=True, allow_infinity=True),
                                 st.booleans(), st.text(max_size=3)), max_size=12))
def test_retry_delays_are_accepted_exactly_when_they_are_up_to_nine_real_seconds(delays):
    ok = len(delays) <= 9 and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                                  and v >= 0 for v in delays)
    with tempfile.TemporaryDirectory() as d:   # settings() reads the file; one per example
        h = Path(d)
        (h / "workflows").mkdir()
        (h / "workflows" / "settings.yaml").write_text(yaml.safe_dump({"webhook_retry_delays": delays}))
        try:
            events.settings(h)
            accepted = True
        except ValueError:
            accepted = False
    assert accepted is ok


def test_webhook_names(hub, monkeypatch):
    settings_file(hub, "webhooks:\n  orders: {verify: header}\n  github-push: {verify: github}\n")
    assert set(events.declarations(hub)) == {"orders", "github-push"}
    for text, message in (
        ("webhooks: [x]\n", "webhooks must be a mapping"),
        ("webhooks:\n  Bad_Name: {}\n", "lowercase URL-safe"),
        ("webhooks:\n  whatsapp: {}\n", "already a chat or legacy inbound route"),
        ("webhooks:\n  webhook: {}\n", "already a chat or legacy inbound route"),
        ("webhooks:\n  a-b: {}\n  a_b: {}\n", "collide after environment normalization"),
        ("webhooks:\n  x: {verify: magic}\n", "header, hmac or github"),
        ("webhooks:\n  x: {verify: hmac}\n", "signed timestamp_header"),
    ):
        settings_file(hub, text)
        with pytest.raises(ValueError, match=message):
            events.declarations(hub)
    assert events.route_declarations(hub) == {}               # best effort: errors disable routes
    (hub / ".env").write_text("WEBHOOK_INBOUND_NAME=intake\n")
    settings_file(hub, "webhooks:\n  intake: {}\n")
    with pytest.raises(ValueError, match="already a chat or legacy inbound route"):
        events.declarations(hub)


def test_an_event_is_admitted_once_and_other_content_is_refused(hub, monkeypatch):
    alerts = []
    from hubzoid.workflows import alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "record", lambda *a, **k: alerts.append(a[2]))
    eid = events.admit(hub, "alpha", "orders", "on_order", "k-1", {"body": {"n": 1}})
    assert eid == events.event_id("alpha", "orders", "k-1") and eid.startswith("wh:")
    assert events.admit(hub, "alpha", "orders", "on_order", "k-1", {"body": {"n": 1}}) == eid
    with pytest.raises(ValueError, match="different content"):
        events.admit(hub, "alpha", "orders", "on_order", "k-1", {"body": {"n": 2}})
    assert alerts == ["content_mismatch"]
    row = events.get(hub, eid)
    assert row["state"] == "accepted" and row["workflow"] == "on_order" and row["attempt"] == 0
    with pytest.raises(RuntimeError, match="backlog is full"):
        events.admit(hub, "alpha", "orders", "on_order", "k-2", {"body": {}}, max_pending=1)
    assert events.get(hub, "wh:missing") is None


def test_fail_alerts_once_and_only_a_failed_event_is_redriven(hub, monkeypatch):
    from hubzoid.workflows import alerts as alerts_mod

    raised = []
    monkeypatch.setattr(alerts_mod, "cooled_record", lambda hub_dir, h, kind, source, detail: raised.append(
        (kind, source)))
    owner = SimpleNamespace(assert_owned=lambda: None)
    eid = events.admit(hub, "alpha", "orders", "on_order", "k", {"body": {}})
    with pytest.raises(ValueError, match="Only a failed event"):
        events.redrive(hub, "alpha", eid)
    events.fail(hub, "alpha", events.get(hub, eid), "boom", owner=owner)
    assert events.get(hub, eid)["state"] == "failed" and raised == [("run_failed", f"{eid}:r0")]
    events.redrive(hub, "alpha", eid)
    row = events.get(hub, eid)
    assert row["state"] == "accepted" and row["redrive"] == 1 and row["error"] is None
    with pytest.raises(ValueError, match="Only a failed event"):
        events.redrive(hub, "other-hub", eid)


def test_reconcile_decides_from_each_coordinator_run(hub, monkeypatch):
    import dbos

    monkeypatch.setattr(runtime, "_OWNER", SimpleNamespace(assert_owned=lambda: None))
    monkeypatch.setattr(runtime, "_APP_VERSION", "v2")
    failed = []
    monkeypatch.setattr(events, "fail", lambda hub_dir, h, row, error, owner=None: failed.append((row["id"], error)))
    ids = [events.admit(hub, "alpha", "orders", "on_order", k, {"body": {"k": k}}) for k in "abcde"]
    events.update(hub, ids[3], "running", version="v1", owner=runtime._OWNER)   # old code started it
    runs = {f"{ids[0]}:r0": SimpleNamespace(workflow_id=f"{ids[0]}:r0", status="PENDING", app_version="v1"),
            f"{ids[1]}:r0": SimpleNamespace(workflow_id=f"{ids[1]}:r0", status="ERROR", app_version="v2"),
            f"{ids[2]}:r0": SimpleNamespace(workflow_id=f"{ids[2]}:r0", status="PENDING", app_version="v2")}
    cancelled, queued = [], []
    fake = SimpleNamespace(
        list_workflows=lambda workflow_ids, **kw: [runs[i] for i in workflow_ids if i in runs],
        cancel_workflow=cancelled.append,
        enqueue_workflow_with_options=lambda options, *args: queued.append((options["workflow_id"], args)))
    monkeypatch.setattr(dbos, "DBOS", fake)
    events.reconcile(hub, "alpha")
    assert cancelled == [f"{ids[0]}:r0"] and events.get(hub, ids[0])["redrive"] == 1   # old code, no attempt
    assert (ids[1], "Coordinator stopped; inspect side effects before redrive") in failed
    assert (ids[3], "Code changed during this event; inspect side effects before redrive") in failed
    assert queued == [(f"{ids[4]}:r0", (ids[4], 0))]                                    # no run yet: start one
    assert ids[2] not in [f for f, _ in failed] and all(q[0] != f"{ids[2]}:r0" for q in queued)
