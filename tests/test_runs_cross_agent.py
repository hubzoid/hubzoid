"""Cross-agent Runs history over the real DBOS query contract.

These tests seed the DBOS system database's ``workflow_status`` table directly and
then read it back through ``observe.runs`` / ``observe.runs_across`` (which open a
real ``DBOSClient`` and call the real ``list_workflows``). This exercises the actual
SDK filter/sort/pagination path — not a re-implementation — while giving the test
full control of timestamps for timezone-boundary and beyond-first-page cases.

DBOS system DBs are per-bridge on SQLite, so each agent gets its own file: exactly
the topology the cross-agent adapter must merge across.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

import hubzoid.db as db
from hubzoid.workflows import observe
from hubzoid.workflows.runtime import _app_name

DBOSClient = pytest.importorskip("dbos").DBOSClient


def _ms(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def _seed(url, app, rows):
    """Insert workflow_status rows for one agent's DBOS DB. rows: dicts with
    id, name, status, created_ms, completed_ms (optional), dequeued_ms (optional,
    → started_at_epoch_ms so a run can be created early but dequeued late), output,
    error."""
    client = DBOSClient(system_database_url=url, application_name=app, retry_connection_errors=False)
    sd = client._sys_db
    sd.run_migrations()
    eng = sd.engine
    with eng.begin() as conn:
        for r in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO workflow_status "
                    "(workflow_uuid, status, name, application_name, created_at, "
                    " updated_at, completed_at, started_at_epoch_ms, output, error, "
                    " recovery_attempts) "
                    "VALUES (:id, :status, :name, :app, :created, :updated, "
                    " :completed, :dequeued, :output, :error, 0)"
                ),
                {
                    "id": r["id"],
                    "status": r["status"],
                    "name": r["name"],
                    "app": app,
                    "created": r["created_ms"],
                    "updated": r.get("completed_ms") or r["created_ms"],
                    "completed": r.get("completed_ms"),
                    "dequeued": r.get("dequeued_ms"),
                    "output": r.get("output"),
                    "error": r.get("error"),
                },
            )
    client.destroy()


@pytest.fixture()
def agents(tmp_path, monkeypatch):
    """Three agents, each with its own SQLite DBOS DB (per-bridge topology)."""
    urls = {}
    hubs = []
    for key in ("finance", "support", "itops"):
        d = tmp_path / key
        d.mkdir()
        urls[str(d.resolve())] = f"sqlite:///{tmp_path / key}-dbos.db"
        hubs.append({"key": key, "name": key.title(), "path": str(d.resolve())})

    def fake_url(hub_dir, env=None):
        return urls[str(hub_dir.resolve() if hasattr(hub_dir, "resolve") else hub_dir)]

    monkeypatch.setattr(db, "dbos_url", fake_url)

    def seed(key, rows):
        d = tmp_path / key
        _seed(fake_url(d), _app_name(d.name), rows)

    return type("A", (), {"hubs": hubs, "seed": staticmethod(seed), "tmp": tmp_path})()


def test_cross_agent_ordering_and_pagination_with_duplicate_names(agents):
    # Interleaved timelines across two agents, plus a workflow NAME shared by both.
    agents.seed(
        "finance",
        [
            {"id": "f1", "name": "sync", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1),
             "completed_ms": _ms(2026, 9, 1, 0, 1), "output": "ok"},
            {"id": "f2", "name": "close", "status": "SUCCESS", "created_ms": _ms(2026, 9, 3),
             "completed_ms": _ms(2026, 9, 3, 0, 1), "output": "ok"},
            {"id": "f3", "name": "close", "status": "ERROR", "created_ms": _ms(2026, 9, 5),
             "completed_ms": _ms(2026, 9, 5, 0, 1), "error": "boom"},
        ],
    )
    agents.seed(
        "support",
        [
            {"id": "s1", "name": "sync", "status": "SUCCESS", "created_ms": _ms(2026, 9, 2),
             "completed_ms": _ms(2026, 9, 2, 0, 1), "output": "ok"},
            {"id": "s2", "name": "digest", "status": "SUCCESS", "created_ms": _ms(2026, 9, 4),
             "completed_ms": _ms(2026, 9, 4, 0, 1), "output": "ok"},
        ],
    )
    hubs = [h for h in agents.hubs if h["key"] in ("finance", "support")]

    # Global newest-first order across both agents.
    page1 = observe.runs_across(hubs, limit=2, offset=0)
    assert [r["id"] for r in page1["runs"]] == ["f3", "s2"]
    assert page1["has_more"] is True

    # Page 2 contains matches beyond the first page — no gaps, no duplicates.
    page2 = observe.runs_across(hubs, limit=2, offset=2)
    assert [r["id"] for r in page2["runs"]] == ["f2", "s1"]
    assert page2["has_more"] is True

    page3 = observe.runs_across(hubs, limit=2, offset=4)
    assert [r["id"] for r in page3["runs"]] == ["f1"]
    assert page3["has_more"] is False

    # The full sequence is strictly descending by start time, across agents.
    everything = observe.runs_across(hubs, limit=50, offset=0)["runs"]
    starts = [r["started"] for r in everything]
    assert starts == sorted(starts, reverse=True)
    assert [r["id"] for r in everything] == ["f3", "s2", "f2", "s1", "f1"]

    # Duplicate workflow name "sync" resolves to the right agent on each row.
    sync = {r["id"]: r["hub"] for r in everything if r["name"] == "sync"}
    assert sync == {"f1": "finance", "s1": "support"}


def test_queued_run_paginates_by_created_not_dequeued(agents):
    """Regression (review #1): a run CREATED earlier but DEQUEUED most recently must
    order and paginate by created_at (what DBOS limits by), so it never appears in the
    full result yet vanishes from a page. Ordering by dequeued would break that."""
    agents.seed(
        "finance",
        [
            # Created newest → on page 1.
            {"id": "new", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 5)},
            {"id": "mid", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 3)},
            # Created OLDEST but dequeued most recently: must sort LAST (by created),
            # i.e. on page 2 — not float to the top.
            {"id": "old-but-late", "name": "w", "status": "SUCCESS",
             "created_ms": _ms(2026, 9, 1), "dequeued_ms": _ms(2026, 9, 9)},
        ],
    )
    hubs = [h for h in agents.hubs if h["key"] == "finance"]
    full = observe.runs_across(hubs, limit=50)["runs"]
    assert [r["id"] for r in full] == ["new", "mid", "old-but-late"]
    # The row's displayed start still reflects the (late) dequeue time...
    late = next(r for r in full if r["id"] == "old-but-late")
    assert late["started"] == _ms(2026, 9, 9)
    # ...but paging (by created) is stable: page 1 then page 2, no gap, no duplicate.
    page1 = observe.runs_across(hubs, limit=2, offset=0)
    page2 = observe.runs_across(hubs, limit=2, offset=2)
    assert [r["id"] for r in page1["runs"]] == ["new", "mid"]
    assert page1["has_more"] is True
    assert [r["id"] for r in page2["runs"]] == ["old-but-late"]
    assert page2["has_more"] is False
    seen = [r["id"] for r in page1["runs"]] + [r["id"] for r in page2["runs"]]
    assert len(seen) == len(set(seen)) == 3  # no duplicates across pages


def test_equal_timestamp_pagination_is_stable(agents):
    """Regression (review #1, exact reproduction): one source, 10 runs sharing an
    identical created_at, ids inserted j..a, page size 2. The initial over-fetch
    truncates the tied group, so re-sorting only the window by id would skip and
    duplicate rows across pages (observed: c,b,c,b,c,b,c,b,b,a). Completing the
    boundary group first makes concatenated pages reproduce the full result exactly."""
    same = _ms(2026, 9, 4, 12, 0)
    agents.seed(
        "finance",
        [{"id": c, "name": "w", "status": "SUCCESS", "created_ms": same} for c in "jihgfedcba"],
    )
    hubs = [h for h in agents.hubs if h["key"] == "finance"]
    full = [r["id"] for r in observe.runs_across(hubs, limit=50)["runs"]]
    assert full == list("jihgfedcba")  # id desc tie-break
    walked = []
    for off in range(0, 10, 2):
        page = observe.runs_across(hubs, limit=2, offset=off)
        walked += [r["id"] for r in page["runs"]]
        assert page["has_more"] is (off + 2 < 10)
    assert walked == full  # no skips, no duplicates
    assert len(walked) == len(set(walked)) == 10


def test_equal_timestamp_pagination_across_two_sources(agents):
    """The same boundary-group completion holds when the tied rows are split across
    two agents on separate DBOS DBs (cross-source merge)."""
    same = _ms(2026, 9, 4, 12, 0)
    agents.seed("finance", [{"id": f"f{c}", "name": "w", "status": "SUCCESS", "created_ms": same} for c in "531"])
    agents.seed("support", [{"id": f"s{c}", "name": "w", "status": "SUCCESS", "created_ms": same} for c in "42"])
    hubs = [h for h in agents.hubs if h["key"] in ("finance", "support")]
    full = [r["id"] for r in observe.runs_across(hubs, limit=50)["runs"]]
    assert full == sorted(["f5", "f3", "f1", "s4", "s2"], reverse=True)
    walked = []
    for off in (0, 2, 4):
        walked += [r["id"] for r in observe.runs_across(hubs, limit=2, offset=off)["runs"]]
    assert walked == full
    assert len(walked) == len(set(walked)) == 5


def test_status_filter_applies_before_pagination(agents):
    # The only failure is the OLDEST run; a status filter must still surface it
    # even with limit=1 (proving the filter runs server-side, before paging).
    agents.seed(
        "finance",
        [
            {"id": "old-fail", "name": "close", "status": "ERROR", "created_ms": _ms(2026, 1, 1),
             "completed_ms": _ms(2026, 1, 1, 0, 1), "error": "boom"},
        ]
        + [
            {"id": f"ok{i}", "name": "close", "status": "SUCCESS",
             "created_ms": _ms(2026, 6, i + 1), "completed_ms": _ms(2026, 6, i + 1, 0, 1), "output": "ok"}
            for i in range(5)
        ],
    )
    hubs = [h for h in agents.hubs if h["key"] == "finance"]
    failed = observe.runs_across(hubs, statuses="failed", limit=1, offset=0)
    assert [r["id"] for r in failed["runs"]] == ["old-fail"]
    assert failed["has_more"] is False


def test_date_window_and_timezone_boundary(agents):
    # created_at is UTC epoch ms; the window is [start, end] inclusive on both ends.
    agents.seed(
        "finance",
        [
            {"id": "before", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 3, 1, 23, 59)},
            {"id": "edge", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 3, 2, 0, 0)},
            {"id": "inside", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 3, 2, 12, 0)},
            {"id": "after", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 3, 3, 0, 1)},
        ],
    )
    hubs = [h for h in agents.hubs if h["key"] == "finance"]
    start = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc).isoformat()
    end = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc).isoformat()
    got = {r["id"] for r in observe.runs_across(hubs, start=start, end=end, limit=50)["runs"]}
    assert got == {"edge", "inside"}


def test_unauthorized_agent_is_excluded(agents):
    agents.seed("finance", [
        {"id": "f", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1)},
    ])
    agents.seed("itops", [
        {"id": "secret", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 2)},
    ])
    # Only finance is authorized: itops' newer run must never appear.
    hubs = [h for h in agents.hubs if h["key"] == "finance"]
    ids = {r["id"] for r in observe.runs_across(hubs, limit=50)["runs"]}
    assert ids == {"f"}


def test_run_id_lookup_across_agents(agents):
    agents.seed("finance", [
        {"id": "f1", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1)},
    ])
    agents.seed("support", [
        {"id": "s1", "name": "w", "status": "ERROR", "created_ms": _ms(2026, 9, 2), "error": "x"},
    ])
    hubs = [h for h in agents.hubs if h["key"] in ("finance", "support")]
    got = observe.runs_across(hubs, run_id="s1", limit=50)
    assert [r["id"] for r in got["runs"]] == ["s1"]
    assert got["has_more"] is False


def test_single_agent_runs_with_status_filter_and_steps(agents):
    agents.seed("finance", [
        {"id": "f1", "name": "close", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1),
         "completed_ms": _ms(2026, 9, 1, 0, 1), "output": "ok"},
        {"id": "f2", "name": "close", "status": "ERROR", "created_ms": _ms(2026, 9, 2),
         "completed_ms": _ms(2026, 9, 2, 0, 1), "error": "boom"},
    ])
    path = agents.tmp / "finance"
    only_failed = observe.runs(path, statuses="failed")
    assert [r["id"] for r in only_failed] == ["f2"]
    # run_id path returns a steps list (empty here — no operation_outputs seeded).
    detail = observe.runs(path, run_id="f1")
    assert detail and detail[0]["id"] == "f1"
    assert "steps" in detail[0]


def test_portal_runs_endpoint_scopes_to_manageable_agents(agents, monkeypatch):
    """The /runs endpoint, with no agent, returns cross-agent history restricted to
    the caller's manageable agents; a named agent they don't manage is 403."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    import hubzoid.access as access
    from hubzoid import deployment
    from hubzoid.portal import PortalAdmin, build_router

    agents.seed("finance", [
        {"id": "f1", "name": "close", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1),
         "completed_ms": _ms(2026, 9, 1, 0, 1), "output": "ok"},
    ])
    agents.seed("itops", [
        {"id": "i1", "name": "patch", "status": "ERROR", "created_ms": _ms(2026, 9, 3),
         "completed_ms": _ms(2026, 9, 3, 0, 1), "error": "x"},
    ])

    monkeypatch.setattr(deployment, "hubs", lambda hub_dir: agents.hubs)
    monkeypatch.setattr(
        deployment, "hub_path",
        lambda hub_dir, key: next(__import__("pathlib").Path(h["path"]) for h in agents.hubs if h["key"] == key),
    )
    eng = create_engine(f"sqlite:///{agents.tmp / 'ops.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()

    who = {"admin": PortalAdmin(subject="root", is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(agents.tmp, admin_resolver=lambda _r: who["admin"]))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})

    # Org admin, cross-agent: both agents' runs, newest first.
    body = c.get("/portal/api/runs").json()
    assert [r["id"] for r in body["runs"]] == ["i1", "f1"]

    # Status filter passes through to the query.
    failed = c.get("/portal/api/runs", params={"status": "failed"}).json()
    assert [r["id"] for r in failed["runs"]] == ["i1"]

    # Hub admin scoped to finance: itops never appears, and naming it is refused.
    who["admin"] = PortalAdmin(subject="ha", is_org_admin=False, manageable=["finance"])
    scoped = c.get("/portal/api/runs").json()
    assert [r["id"] for r in scoped["runs"]] == ["f1"]
    assert c.get("/portal/api/runs", params={"hub": "itops"}).status_code == 403


def test_resolve_statuses_buckets_passthrough_and_rejects_invalid():
    assert observe.resolve_statuses("failed") == ["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"]
    assert observe.resolve_statuses("running") == ["PENDING", "ENQUEUED"]
    assert observe.resolve_statuses("succeeded") == ["SUCCESS"]
    # Comma list + raw DBOS-state pass-through.
    assert observe.resolve_statuses("succeeded,failed") == [
        "SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    ]
    assert observe.resolve_statuses("SUCCESS") == ["SUCCESS"]
    # Absent filter → None (no filtering). Whitespace/empty is absent, not invalid.
    assert observe.resolve_statuses(None) is None
    assert observe.resolve_statuses("") is None
    assert observe.resolve_statuses("  ") is None
    # Regression (review #3): a supplied-but-unknown filter must NOT collapse to None
    # (which would widen to every status) — it raises so the caller can 422 it. A mix
    # with any bad token is rejected too.
    with pytest.raises(ValueError):
        observe.resolve_statuses("bogus")
    with pytest.raises(ValueError):
        observe.resolve_statuses("succeeded,bogus")


def test_portal_runs_endpoint_rejects_invalid_status(agents, monkeypatch):
    """Regression (review #3): an unknown status filter is a 422, and returns no runs
    — never a silently-unfiltered 200 that leaks SUCCESS runs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    import hubzoid.access as access
    from hubzoid import deployment
    from hubzoid.portal import PortalAdmin, build_router

    agents.seed("finance", [
        {"id": "f1", "name": "w", "status": "SUCCESS", "created_ms": _ms(2026, 9, 1),
         "completed_ms": _ms(2026, 9, 1, 0, 1), "output": "ok"},
    ])
    monkeypatch.setattr(deployment, "hubs", lambda hub_dir: agents.hubs)
    monkeypatch.setattr(
        deployment, "hub_path",
        lambda hub_dir, key: next(__import__("pathlib").Path(h["path"]) for h in agents.hubs if h["key"] == key),
    )
    eng = create_engine(f"sqlite:///{agents.tmp / 'ops2.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    app = FastAPI()
    app.include_router(build_router(
        agents.tmp,
        admin_resolver=lambda _r: PortalAdmin(subject="root", is_org_admin=True, manageable=[]),
    ))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    assert c.get("/portal/api/runs", params={"status": "nonsense"}).status_code == 422
    # A valid filter still works and returns the SUCCESS run.
    ok = c.get("/portal/api/runs", params={"status": "succeeded"})
    assert ok.status_code == 200 and [r["id"] for r in ok.json()["runs"]] == ["f1"]
