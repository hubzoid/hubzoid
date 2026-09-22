"""Direct tests for the atomic access-apply path and the consistent snapshot.

SQLite, pure (casbin + sqlite, no LLM/network). PostgreSQL variants of the
concurrency cases live in tests/test_postgres_acceptance.py.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import create_engine

from hubzoid.access.store import (
    USE_HUB,
    GrantStore,
    RevisionConflict,
)


def _engine(tmp_path):
    # A shared on-disk DB so multiple GrantStore "processes" hit one file; a
    # busy timeout lets concurrent writers wait for the lock instead of erroring.
    return create_engine(
        f"sqlite:///{tmp_path / 'hub.db'}", connect_args={"timeout": 30}
    )


@pytest.fixture()
def store(tmp_path):
    return GrantStore(_engine(tmp_path))


def test_apply_is_atomic_and_bumps_revision_once(store):
    rev0 = store.revision()
    new_rev = store.apply_changes(
        "alice", "finance",
        [("grant", "ledger"), ("grant", "invoices")],
        expected_revision=rev0, actor="admin",
    )
    assert new_rev == rev0 + 1  # one revision bump for the whole set
    assert store.permissions_for("alice", "finance") == {"ledger", "invoices", USE_HUB}


def test_apply_expected_revision_mismatch_conflicts(store):
    rev0 = store.revision()
    store.grant("bob", "finance", USE_HUB)  # someone else moves the revision
    with pytest.raises(RevisionConflict):
        store.apply_changes(
            "alice", "finance", [("grant", "ledger")],
            expected_revision=rev0, actor="admin",
        )
    assert store.permissions_for("alice", "finance") == set()  # nothing applied


def test_apply_none_expected_revision_skips_guard(store):
    store.apply_changes("alice", "finance", [("grant", "ledger")], expected_revision=None)
    assert store.can("alice", "finance", "ledger")


def test_apply_cascade_removes_all_in_hub_atomically(store):
    store.apply_changes("alice", "finance", [("grant", "ledger"), ("grant", "invoices")])
    rev = store.revision()
    store.apply_changes(
        "alice", "finance", [("revoke", USE_HUB)], expected_revision=rev
    )
    assert store.permissions_for("alice", "finance") == set()


def test_apply_can_revoke_an_orphan_permission(store):
    # A tool later removed from the catalogue still has a grant row; apply must be
    # able to revoke it (the endpoint allows revoke of an out-of-catalogue perm).
    store.grant("alice", "finance", "legacy_tool")
    assert "legacy_tool" in store.permissions_for("alice", "finance")
    rev = store.revision()
    store.apply_changes(
        "alice", "finance", [("revoke", "legacy_tool")], expected_revision=rev
    )
    assert "legacy_tool" not in store.permissions_for("alice", "finance")


def test_apply_rolls_back_grants_audit_and_revision_on_midtxn_failure(store, monkeypatch):
    store.grant("alice", "finance", "ledger")  # pre-existing
    rev_before = store.revision()
    audit_before = len(store.read_access_audit())

    # Fail on the SECOND insert within the transaction.
    real_insert = store._insert_grant
    calls = {"n": 0}

    def flaky_insert(conn, s, h, p):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom mid-transaction")
        return real_insert(conn, s, h, p)

    monkeypatch.setattr(store, "_insert_grant", flaky_insert)
    with pytest.raises(RuntimeError):
        store.apply_changes(
            "alice", "finance",
            [("grant", "invoices"), ("grant", "payroll")],
            expected_revision=rev_before, actor="admin",
        )
    # Whole set rolled back: no new perms, revision unchanged, no audit rows.
    assert store.permissions_for("alice", "finance") == {"ledger", USE_HUB}
    assert store.revision() == rev_before
    assert len(store.read_access_audit()) == audit_before


def test_two_simultaneous_applies_same_revision_exactly_one_wins(tmp_path):
    a = GrantStore(_engine(tmp_path))
    b = GrantStore(_engine(tmp_path))
    rev = a.revision()
    barrier = threading.Barrier(2)

    def apply(store, subject):
        barrier.wait()
        try:
            store.apply_changes(
                subject, "finance", [("grant", "ledger")],
                expected_revision=rev, actor="admin",
            )
            return "ok"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(apply, a, "alice")
        f2 = ex.submit(apply, b, "bob")
        results = sorted([f1.result(), f2.result()])
    assert results == ["conflict", "ok"], results


def test_apply_vs_other_writer_conflicts(tmp_path):
    # apply must also lose to a plain grant()/suspend() that moved the revision,
    # not just to another apply().
    a = GrantStore(_engine(tmp_path))
    b = GrantStore(_engine(tmp_path))
    rev = a.revision()
    b.grant("carol", "finance", USE_HUB)  # another policy writer moves on
    with pytest.raises(RevisionConflict):
        a.apply_changes(
            "alice", "finance", [("grant", "ledger")],
            expected_revision=rev, actor="admin",
        )


def test_concurrent_snapshot_reads_stay_consistent(tmp_path):
    writer = GrantStore(_engine(tmp_path))
    reader = GrantStore(_engine(tmp_path))
    rev0, grants0 = reader.access_snapshot()
    base = len(grants0)
    N = 40
    stop = threading.Event()
    seen: list[tuple[int, int]] = []

    def read_loop():
        while not stop.is_set():
            rev, grants = reader.access_snapshot()
            seen.append((rev - rev0, len(grants) - base))

    t = threading.Thread(target=read_loop)
    t.start()
    try:
        for i in range(N):
            # Each grant of a distinct subject's use_hub adds exactly one row and
            # bumps the revision once — so (rev delta) must equal (grant delta).
            writer.grant(f"user{i}", "finance", USE_HUB)
    finally:
        stop.set()
        t.join()
    # Take a few more after writes settle.
    for _ in range(5):
        rev, grants = reader.access_snapshot()
        seen.append((rev - rev0, len(grants) - base))
    assert seen, "reader captured no snapshots"
    for d_rev, d_grants in seen:
        assert d_rev == d_grants, f"inconsistent snapshot: rev+{d_rev} but grants+{d_grants}"
    assert (N, N) in seen  # the final settled state was observed
