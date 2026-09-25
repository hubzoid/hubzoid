"""The Console home (`/portal/api/summary`): chat-first numbers per hub from
Hubzoid's own tables. Unknown numbers are null, never zero."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import migrations
from hubzoid.portal import PortalAdmin, build_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    hub = tmp_path / "support"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: support\ndescription: d\n---\nbody")
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    monkeypatch.setenv("HUBZOID_DBOS_DB", f"sqlite:///{tmp_path / 'dbos.db'}")
    access._stores.clear()
    migrations._done.clear()
    migrations.upgrade(eng, "operational")
    gs = access.store_for(hub)
    admin = {"who": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(hub, admin_resolver=lambda _r: admin["who"]))
    c = TestClient(app)
    c.eng, c.gs, c.hub, c.admin = eng, gs, hub, admin  # type: ignore[attr-defined]
    return c


def _usage(eng, **row):
    base = dict(ts=time.time(), hub="support", surface="web", kind="chat", subject=None,
                chat_id=None, model="m", input_tokens=None, output_tokens=None,
                cost_usd=None, status="ok", duration_ms=10)
    base.update(row)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, input_tokens, "
            "output_tokens, cost_usd, status, duration_ms) VALUES (:ts, :hub, :surface, :kind, "
            ":subject, :chat_id, :model, :input_tokens, :output_tokens, :cost_usd, :status, "
            ":duration_ms)"), base)


def test_chat_numbers_per_period(client):
    now = time.time()
    _usage(client.eng, subject="a@x.org", chat_id="c1", input_tokens=100, output_tokens=20, cost_usd=0.01)
    _usage(client.eng, subject="a@x.org", chat_id="c1", input_tokens=50, output_tokens=10, cost_usd=0.02)
    _usage(client.eng, subject="b@x.org", chat_id="c2", surface="slack", input_tokens=10, output_tokens=5)
    _usage(client.eng, kind="llm", surface="workflow", subject="workflow:digest",
           input_tokens=1000, output_tokens=100, cost_usd=0.5)
    _usage(client.eng, ts=now - 3 * 86400, subject="c@x.org", chat_id="c3", input_tokens=1, output_tokens=1)

    day = client.get("/portal/api/summary", params={"period": "24h"}).json()
    (row,) = day["hubs"]
    assert (row["messages"], row["chats"], row["active_users"]) == (3, 2, 2)
    assert (row["input_tokens"], row["output_tokens"]) == (1160, 135)   # includes the workflow call
    assert row["cost_usd"] == pytest.approx(0.53) and row["unpriced"] == 1
    assert day["totals"]["active_users"] == 2
    assert day["recording_since"] == pytest.approx(now - 3 * 86400, abs=5)

    week = client.get("/portal/api/summary", params={"period": "7d"}).json()
    assert week["totals"]["messages"] == 4 and week["totals"]["active_users"] == 3
    assert client.get("/portal/api/summary", params={"period": "1y"}).status_code == 422


def test_no_usage_reads_as_unknown_not_zero_cost(client):
    body = client.get("/portal/api/summary").json()
    assert body["recording_since"] is None
    assert body["totals"]["cost_usd"] is None
    (row,) = body["hubs"]
    assert row["cost_usd"] is None and row["last_activity"] is None


def test_access_numbers_and_denials(client):
    hub = client.hub.name
    client.gs.set_authoritative(True, hub=hub)
    client.gs.grant("a@x.org", hub, "use_hub")
    client.gs.grant("b@x.org", hub, "use_hub")
    client.gs.grant("workflow:digest", hub, "use_hub")   # services are not people
    from hubzoid.access import audit

    audit.record(client.hub, user="b@x.org", surface="owui", tool="t", decision="deny", reason="no-grant")
    audit.record(client.hub, user="a@x.org", surface="owui", tool="t", decision="allow", reason="grant")
    (row,) = client.get("/portal/api/summary").json()["hubs"]
    assert row["users_with_access"] == 2 and row["everyone"] is False
    assert row["denials"] == 1

    client.gs.set_authoritative(False, hub=hub)
    (row,) = client.get("/portal/api/summary").json()["hubs"]
    assert row["users_with_access"] is None   # legacy access lives in the chat app


def test_workflow_columns_only_for_hubs_with_work(client):
    body = client.get("/portal/api/summary").json()
    assert body["has_workflows"] is False and body["totals"]["runs"] is None
    assert body["hubs"][0]["runs"] is None
    assert body["hubs"][0]["missed"] is None and body["totals"]["missed"] is None

    (client.hub / "schedule").mkdir()
    (client.hub / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    body = client.get("/portal/api/summary").json()
    assert body["has_workflows"] is True
    assert body["hubs"][0]["has_workflows"] is True
    assert body["totals"]["runs"] == 0   # no DBOS database yet: nothing has run
    assert body["hubs"][0]["missed"] == 0 and body["totals"]["missed"] == 0   # no records yet


def _ago(seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()


def test_missed_slots_are_counted_per_period(client):
    """Both dispatchers keep dated [time, count] records of skipped slots: code
    workflows in the hub's runtime health, markdown tasks in schedule state."""
    import json

    from hubzoid.scheduling import ScheduleState

    (client.hub / "schedule").mkdir()
    (client.hub / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    state = ScheduleState(client.hub)
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text(json.dumps({"daily": {"missed_log": [
        [_ago(2 * 3600), 2], [_ago(3 * 86400), 5], ["not a time", 9]]}}))
    client.gs.set_runtime_health(client.hub.name, missed_log=[[_ago(3600), 1], [_ago(10 * 86400), 4]])

    def missed(period):
        body = client.get("/portal/api/summary", params={"period": period}).json()
        assert body["totals"]["missed"] == body["hubs"][0]["missed"]
        return body["hubs"][0]["missed"]

    assert (missed("24h"), missed("7d"), missed("30d")) == (3, 8, 12)


def test_a_hub_admin_sees_only_their_hubs(client):
    client.admin["who"] = PortalAdmin(subject="h@x.org", is_org_admin=False, manageable=[])
    body = client.get("/portal/api/summary").json()
    assert body["hubs"] == [] and body["totals"]["messages"] == 0


def test_recording_since_comes_only_from_the_given_hubs(client):
    """A hub admin never sees when another hub started recording."""
    from hubzoid import usage

    now = time.time()
    _usage(client.eng, hub="other", ts=now - 20 * 86400, subject="x@x.org", chat_id="o1")
    assert usage.summary(client.eng, {"support": "support"}, now - 86400)["recording_since"] is None
    _usage(client.eng, ts=now - 3600, subject="a@x.org", chat_id="c1")
    got = usage.summary(client.eng, {"support": "support"}, now - 86400)["recording_since"]
    assert got == pytest.approx(now - 3600, abs=5)
    both = usage.summary(client.eng, {"support": "support", "other": "other"}, now - 86400)
    assert both["recording_since"] == pytest.approx(now - 20 * 86400, abs=5)
