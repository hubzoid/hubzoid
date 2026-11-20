"""The Usage page (`/portal/api/usage`, `/portal/api/usage/people`) and the
usage details every writer now records: what started a call (origin), cache
and reasoning tokens, and time to the first answer text. No model calls."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import migrations
from hubzoid import usage as usage_lib
from hubzoid.portal import PortalAdmin, build_router

HOUR, DAY = 3600, 86400


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
    access.store_for(hub)
    admin = {"who": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(hub, admin_resolver=lambda _r: admin["who"]))
    c = TestClient(app)
    c.eng, c.hub, c.admin = eng, hub, admin  # type: ignore[attr-defined]
    return c


def _row(eng, **row):
    base = dict(ts=time.time(), hub="support", surface="web", kind="chat", subject="ann@x.org",
                chat_id="c1", model="claude-sonnet", input_tokens=100, output_tokens=10, cost_usd=0.01,
                status="ok", duration_ms=1000, origin="chat", cache_read_tokens=60, cache_write_tokens=0,
                reasoning_tokens=None, first_token_ms=None)
    base.update(row)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, input_tokens, output_tokens,"
            " cost_usd, status, duration_ms, origin, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
            " first_token_ms) VALUES (:ts, :hub, :surface, :kind, :subject, :chat_id, :model, :input_tokens,"
            " :output_tokens, :cost_usd, :status, :duration_ms, :origin, :cache_read_tokens,"
            " :cache_write_tokens, :reasoning_tokens, :first_token_ms)"), base)


def test_the_page_reads_one_period_with_buckets_breakdowns_and_times(client):
    now = time.time()
    _row(client.eng, first_token_ms=800, duration_ms=2000)
    _row(client.eng, ts=now - 2 * HOUR, subject="bo@x.org", chat_id="c2", surface="slack", duration_ms=4000,
         status="error")
    _row(client.eng, ts=now - 3 * HOUR, kind="agent", surface="workflow", subject="workflow:digest",
         origin="schedule", model="gpt-5-mini", input_tokens=1000, output_tokens=200, cost_usd=None,
         cache_read_tokens=400, reasoning_tokens=50, duration_ms=9000)
    _row(client.eng, ts=now - 3 * DAY, origin=None, duration_ms=500)          # before the column: Earlier
    _row(client.eng, kind="background", duration_ms=1)                         # left out of response times

    day = client.get("/portal/api/usage", params={"period": "24h"}).json()
    t = day["totals"]
    assert (t["calls"], t["messages"], t["chats"], t["people"]) == (4, 2, 2, 2)
    assert (t["input_tokens"], t["output_tokens"], t["cache_read_tokens"]) == (1300, 230, 580)
    assert t["reasoning_tokens"] == 50 and t["errors"] == 1 and t["unpriced"] == 1
    assert day["bucket"] == HOUR and len(day["series"]) == 24
    assert sum(p["input_tokens"] for p in day["series"]) == t["input_tokens"]
    assert sum(p["output_tokens"] for p in day["series"]) == t["output_tokens"]
    assert {r["surface"]: r["calls"] for r in day["by_surface"]} == {"web": 2, "slack": 1, "workflow": 1}
    assert {r["trigger"]: r["calls"] for r in day["by_trigger"]} == {"chat": 3, "schedule": 1}
    models = {r["model"]: r for r in day["by_model"]}
    assert models["gpt-5-mini"]["reasoning_tokens"] == 50 and models["gpt-5-mini"]["cost_usd"] is None
    assert models["claude-sonnet"]["reasoning_tokens"] is None          # Claude reports none
    (hub,) = day["by_hub"]
    assert hub["key"] == "support" and hub["errors"] == 1
    # Failed and background calls are not response times: 4000 ms (error) and 1 ms
    # (background) are out, leaving 2000 and 9000.
    assert day["latency"] == {"first_token_p50_ms": 800, "p50_ms": 2000, "p95_ms": 9000}
    assert day["previous"]["calls"] == 0     # recording started 3 days ago: the day before is known

    week = client.get("/portal/api/usage", params={"period": "7d"}).json()
    assert week["bucket"] == DAY and len(week["series"]) == 7
    assert {r["trigger"]: r["calls"] for r in week["by_trigger"]}[None] == 1
    assert week["totals"]["calls"] == 5
    assert week["previous"] is None           # but not the week before
    assert client.get("/portal/api/usage", params={"period": "1y"}).status_code == 422


def test_the_previous_period_is_compared_only_when_fully_recorded(client):
    now = time.time()
    _row(client.eng, ts=now - 49 * HOUR, input_tokens=7)    # recording starts before the earlier day
    _row(client.eng, ts=now - 30 * HOUR, input_tokens=40)
    _row(client.eng, ts=now - HOUR, input_tokens=100)
    day = client.get("/portal/api/usage", params={"period": "24h"}).json()
    assert day["previous"]["input_tokens"] == 40 and day["totals"]["input_tokens"] == 100


def test_the_agent_filter_and_the_admin_scope(client):
    _row(client.eng)
    _row(client.eng, hub="elsewhere", input_tokens=9999)      # another hub's row never counts
    assert client.get("/portal/api/usage", params={"hub": "support"}).json()["totals"]["input_tokens"] == 100
    assert client.get("/portal/api/usage").json()["totals"]["input_tokens"] == 100
    assert client.get("/portal/api/usage", params={"hub": "nope"}).status_code == 404
    client.admin["who"] = PortalAdmin(subject="d@x.org", is_org_admin=False, manageable=[])
    assert client.get("/portal/api/usage").json()["totals"]["calls"] == 0
    assert client.get("/portal/api/usage", params={"hub": "support"}).status_code == 404
    assert client.get("/portal/api/usage/people").json() == {"period": "7d", "page": 0, "size": 8,
                                                             "rows": [], "total": 0}


def test_people_are_searched_sorted_and_paged(client):
    for i in range(11):
        _row(client.eng, subject=f"p{i:02d}@x.org", chat_id=f"c{i}", cost_usd=0.01 * (i + 1))
    _row(client.eng, subject="workflow:md:daily", kind="agent", surface="workflow", origin="schedule",
         cost_usd=1.0)
    first = client.get("/portal/api/usage/people", params={"size": 5}).json()
    assert first["total"] == 12 and len(first["rows"]) == 5
    assert first["rows"][0]["subject"] == "workflow:md:daily" and first["rows"][0]["kind"] == "schedule"
    assert first["rows"][0]["messages"] == 0 and first["rows"][1]["subject"] == "p10@x.org"
    last = client.get("/portal/api/usage/people", params={"size": 5, "page": 2}).json()
    assert [r["subject"] for r in last["rows"]] == ["p01@x.org", "p00@x.org"]
    found = client.get("/portal/api/usage/people", params={"q": "P03"}).json()
    assert [r["subject"] for r in found["rows"]] == ["p03@x.org"] and found["rows"][0]["agents"] == ["support"]
    by_name = client.get("/portal/api/usage/people", params={"sort": "name", "order": "asc", "size": 2}).json()
    assert [r["subject"] for r in by_name["rows"]] == ["p00@x.org", "p01@x.org"]
    assert client.get("/portal/api/usage/people", params={"q": "support"}).json()["total"] == 12  # by agent


def test_summary_rows_count_errors(client):
    _row(client.eng, status="error")
    _row(client.eng)
    (row,) = client.get("/portal/api/summary", params={"period": "7d"}).json()["hubs"]
    assert row["errors"] == 1


# -- writers -------------------------------------------------------------------------

def _written(hub):
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    with eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_usage ORDER BY id"))]


def test_record_keeps_details_and_says_what_started_the_call(tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_OPERATIONAL_DB", raising=False)
    migrations._done.clear()
    hub = tmp_path / "ops"
    hub.mkdir()
    rec = dict(hub="ops", kind="chat", subject="a@x.org", model="m", input_tokens=10, output_tokens=2)
    usage_lib.record(hub, surface="web", **rec, **usage_lib.usage_details(
        {"cache_read_tokens": 6, "cache_write_tokens": "2", "reasoning_tokens": -1, "first_token_ms": 700}))
    usage_lib.record(hub, surface="mcp", **rec)
    usage_lib.record(hub, surface="workflow", **rec)                     # untagged workflow: unknown
    with usage_lib.run_tag("run-1", trigger="schedule"):
        usage_lib.record(hub, surface="workflow", **rec)
    usage_lib.record(hub, surface="workflow", trigger="eval", **rec)
    rows = _written(hub)
    assert [r["origin"] for r in rows] == ["chat", "mcp", None, "schedule", "eval"]
    assert (rows[0]["cache_read_tokens"], rows[0]["cache_write_tokens"], rows[0]["first_token_ms"]) == (6, 2, 700)
    assert rows[0]["reasoning_tokens"] is None and rows[3]["chat_id"] == "run-1"


@pytest.mark.parametrize("trigger,via,origin", [
    ("schedule", None, "schedule"), ("personal_schedule", None, "schedule"), ("operator", "console", "manual"),
    ("cli", None, "manual"), ("webhook", None, "event"), ("update", None, "event"), ("work_item", None, "manual"),
    ("tool", "slack", "chat"), ("tool", "mcp", "mcp"), ("operator", "mcp", "mcp"), ("operator", "web", "chat"),
    ("something-new", None, None),
])
def test_a_workflow_run_maps_to_an_origin(trigger, via, origin):
    assert usage_lib.run_trigger(trigger, via) == origin


@pytest.mark.parametrize("run_id,events,origin", [
    ("md:daily:20261120T0900@ops", None, "schedule"),
    ("md:daily:manual-20261120T091500-ab12cd@ops", None, "manual"),
    ("md:hooks:events-20261120T0900@ops:requeued", None, "event"),
    ("md:hooks:20261120T0900@ops", ["e.json"], "event"),
    (None, None, "schedule"),
])
def test_a_markdown_task_run_says_how_it_started(run_id, events, origin):
    assert usage_lib.markdown_origin(run_id, events) == origin


def test_openai_style_usage_details_from_objects_and_dicts():
    obj = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=30),
                          completion_tokens_details=SimpleNamespace(reasoning_tokens=12))
    assert usage_lib.litellm_details(obj) == {"cache_read_tokens": 30, "reasoning_tokens": 12}
    agents_sdk = {"input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 0}}
    assert usage_lib.litellm_details(agents_sdk) == {"cache_read_tokens": 5, "reasoning_tokens": 0}
    assert usage_lib.litellm_details(SimpleNamespace()) == {} and usage_lib.litellm_details(None) == {}


def test_claude_reports_cache_tokens_as_part_of_input():
    from claude_agent_sdk import ResultMessage

    from hubzoid import factory_claude

    msg = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                        session_id="s", result="OK", usage={"input_tokens": 10, "output_tokens": 4,
                                                            "cache_read_input_tokens": 900,
                                                            "cache_creation_input_tokens": 90})
    row = factory_claude.claude_usage(msg, "claude-sonnet-5")
    assert row["input_tokens"] == 1000 and row["cache_read_tokens"] == 900 and row["cache_write_tokens"] == 90
    assert "reasoning_tokens" not in row


def test_codex_turns_sum_details_and_a_tokenless_record_changes_nothing():
    from hubzoid.factory_codex import _combine_usage

    out = _combine_usage([
        {"model": "gpt-5-codex", "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 60,
         "cache_write_tokens": 0, "reasoning_tokens": 8},
        {"model": "gpt-5-codex", "input_tokens": 50, "output_tokens": 10, "cache_read_tokens": 10,
         "reasoning_tokens": 2},
        {},                                   # a delegate that failed before it reported usage
    ])
    assert (out["input_tokens"], out["cache_read_tokens"], out["reasoning_tokens"]) == (150, 70, 10)
    assert out["model"] == "gpt-5-codex" and "status" not in out


def test_an_existing_store_gains_the_columns_and_keeps_its_rows(tmp_path):
    from tests.test_one_home_upgrade import _to

    eng = create_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    _to(eng, "op_0081")
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_usage (ts, hub, surface, kind, status, input_tokens)"
                       " VALUES (1, 'ops', 'web', 'chat', 'ok', 5)"))
    migrations._done.clear()
    migrations.upgrade(eng, "operational")
    columns = {c["name"] for c in inspect(eng).get_columns("hz_usage")}
    assert {"origin", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "first_token_ms"} <= columns
    out = usage_lib.insights(eng, {"ops": "ops"}, 0, 10, 10)
    assert out["totals"]["input_tokens"] == 5 and out["by_trigger"] == [{"trigger": None, "calls": 1,
                                                                         "cost_usd": 0.0}]


@pytest.mark.slow
def test_the_page_queries_run_on_postgres(postgres_url):
    eng = create_engine(postgres_url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    migrations._done.clear()
    migrations.upgrade(eng, "operational")
    now = time.time()
    _row(eng, ts=now - 10, hub="ops", first_token_ms=500)
    _row(eng, ts=now - 2 * HOUR - 1, hub="ops", subject="bo@x.org", chat_id="c2", status="error")
    _row(eng, ts=now - 2 * DAY, hub="ops", kind="agent", surface="workflow", origin="schedule",
         subject="workflow:md:daily", reasoning_tokens=5)
    day = usage_lib.insights(eng, {"ops": "ops"}, now - DAY, now, HOUR)
    assert day["totals"]["calls"] == 2 and day["totals"]["errors"] == 1
    # Postgres rounds a CAST, so the bucket index must floor: the row 2h ago is in bucket 21, not 22.
    assert [i for i, p in enumerate(day["series"]) if p["input_tokens"]] == [21, 23]
    week = usage_lib.insights(eng, {"ops": "ops"}, now - 7 * DAY, now, DAY)
    assert week["totals"]["reasoning_tokens"] == 5 and {r["trigger"] for r in week["by_trigger"]} == {"chat", "schedule"}
    found = usage_lib.people(eng, {"ops": "ops"}, now - 7 * DAY, now, q="daily")
    assert found["total"] == 1 and found["rows"][0]["kind"] == "schedule"
    eng.dispose()
