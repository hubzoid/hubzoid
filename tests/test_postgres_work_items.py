"""Work items, usage limits and sandbox records on PostgreSQL: the migrations,
duplicate starts, concurrent admission under the advisory lock, and one live
sandbox per owner. Starts a local PostgreSQL, so it is marked slow."""
from __future__ import annotations

import textwrap
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from hubzoid import budgets
from hubzoid.work_items import service, store
from hubzoid.work_items.errors import WorkItemError

OWNER = "admin@localhost"


@pytest.fixture
def pg_hub(tmp_path, monkeypatch, postgres_url):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", postgres_url)
    hub = tmp_path / "pghub"
    (hub / "workflows" / "desk").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: Desk\nmodel: claude-local\n---\nA desk.\n")
    (hub / "workflows" / "desk" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import workflow
        from hubzoid.work_items import Route

        @workflow(work_items={"plan": Route(stages=("draft",), actions=())})
        def desk():
            return {}
    '''))
    return hub


@pytest.mark.slow
def test_work_items_limits_and_sandboxes_on_postgres(pg_hub):
    eng = store.engine(pg_hub)
    assert eng.dialect.name == "postgresql"
    with eng.connect() as c:
        tables = set(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
    assert {"hz_work_items", "hz_operations", "hz_station_calls", "hz_handoffs", "hz_sandboxes",
            "hz_sandbox_read_stamps", "hz_budget_reservations"} <= tables

    # A repeated start returns the same item.
    first, created = service.admit(pg_hub, workflow="desk", key="pg:1", requester=OWNER)
    again, created_again = service.admit(pg_hub, workflow="desk", key="pg:1", requester=OWNER)
    assert created and not created_again and again["id"] == first["id"]

    # Two admissions at the same moment against room for one: exactly one runs.
    (pg_hub / "budgets.yaml").write_text("version: 1\nlimits:\n  run:\n    max_output_tokens: 100\n"
                                         "  month:\n    max_output_tokens: 100\n")

    def admit(run):
        try:
            return budgets.admit(pg_hub, model="claude-local", run_id=run)
        except WorkItemError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(admit, ["a", "b"]))
    assert sorted(str(type(r).__name__) if not isinstance(r, str) else r for r in results) == \
        ["Ticket", "budget_exhausted"]

    # One live sandbox per owner; ended ones do not count.
    insert = text("INSERT INTO hz_sandboxes (id, hub, deployment, owner_kind, owner_id, live_key, profile_id, "
                  "profile_digest, profile_json, provider_binding, provider_kind, state, created, "
                  "lifetime_deadline, updated) VALUES (:id, 'pghub', 'd', 'probe', 'p', :lk, 'x', 'x', '{}', "
                  "'local', 'docker', 'ready', 0, 0, 0)")
    with eng.begin() as c:
        c.execute(insert, {"id": "s1", "lk": "probe:p"})
        c.execute(insert, {"id": "s0", "lk": None})
        c.execute(insert, {"id": "s00", "lk": None})
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(insert, {"id": "s2", "lk": "probe:p"})
