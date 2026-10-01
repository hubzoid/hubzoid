"""Groups in the access store: CRUD, and grants to `group:<id>` resolving for
every member through `can`, `permissions_for`, `hubs_for` and `held_since`,
fail closed, with suspension, the public `*` grant and deletion cascading."""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text

from hubzoid.access.store import (
    EVERYONE,
    MANAGE_ACCESS,
    ORG,
    USE_HUB,
    GrantStore,
    GroupError,
    GroupNameTaken,
    GroupNotFound,
    group_id_of,
    group_subject,
    is_group_subject,
)

HUB = "sales"
ADMIN = "boss@example.org"


@pytest.fixture()
def gs(tmp_path):
    store = GrantStore(create_engine(f"sqlite:///{tmp_path / 'op.db'}"))
    store.bootstrap([ADMIN], authoritative=True, hub=HUB)
    store.set_authoritative(True, hub="support")
    return store


def _group(gs, name="Sales team", members=("ann@example.org", "bob@example.org")):
    return gs.create_group(name, actor=ADMIN, emails=members)


def test_subject_helpers():
    assert group_subject("G_1") == "group:g_1"
    assert is_group_subject("group:g_1") and not is_group_subject("ann@example.org")
    assert group_id_of("group:g_1") == "g_1"
    assert group_id_of("group:bad id") is None and group_id_of("ann@example.org") is None


def test_create_lists_and_reads_a_group(gs):
    g = _group(gs, members=("Ann@Example.org", "bob@example.org", "ann@example.org"))
    assert g["id"].startswith("g_") and g["name"] == "Sales team"
    assert [m["email"] for m in g["members"]] == ["ann@example.org", "bob@example.org"]
    [listed] = gs.list_groups()
    assert (listed["member_count"], listed["grant_count"]) == (2, 0)
    assert gs.groups_for("ANN@example.org") == [{"id": g["id"], "name": "Sales team"}]
    assert gs.group("g_missing") is None


def test_names_are_unique_case_insensitively(gs):
    _group(gs)
    with pytest.raises(GroupNameTaken):
        gs.create_group("  sales   TEAM ", actor=ADMIN)
    other = gs.create_group("Support", actor=ADMIN)
    with pytest.raises(GroupNameTaken):
        gs.update_group(other["id"], name="SALES TEAM", actor=ADMIN)
    with pytest.raises(GroupError):
        gs.create_group("   ", actor=ADMIN)


def test_a_group_grant_applies_to_every_member_and_nobody_else(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    for member in ("ann@example.org", "bob@example.org"):
        assert gs.can(member, HUB, "crm_read")
        assert gs.can(member, HUB, USE_HUB)  # the use_hub implication holds for groups
    assert not gs.can("carl@example.org", HUB, "crm_read")
    assert not gs.can("ann@example.org", "support", USE_HUB)
    assert gs.permissions_for("ann@example.org", HUB) == {USE_HUB, "crm_read"}
    assert gs.hubs_for("bob@example.org") == {HUB}


def test_membership_changes_take_effect_on_the_next_decision(gs):
    g = _group(gs, members=("ann@example.org",))
    gs.grant(group_subject(g["id"]), HUB, USE_HUB, actor=ADMIN)
    rev = gs.revision()
    assert gs.add_group_members(g["id"], ["carl@example.org", "ann@example.org"], actor=ADMIN) == ["carl@example.org"]
    assert gs.revision() > rev
    assert gs.can("carl@example.org", HUB, USE_HUB)
    assert gs.remove_group_member(g["id"], "carl@example.org", actor=ADMIN) is True
    assert not gs.can("carl@example.org", HUB, USE_HUB)
    assert gs.remove_group_member(g["id"], "carl@example.org", actor=ADMIN) is False


def test_another_process_sees_membership_changes(tmp_path, gs):
    """Two stores over one database (two bridges): a membership written by one
    is honoured by the other on its next decision (the policy revision)."""
    other = GrantStore(create_engine(f"sqlite:///{tmp_path / 'op.db'}"))
    g = _group(gs, members=())
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    assert not other.can("dee@example.org", HUB, "crm_read")
    gs.add_group_members(g["id"], ["dee@example.org"], actor=ADMIN)
    assert other.can("dee@example.org", HUB, "crm_read")


def test_suspension_wins_over_groups_and_blocking_leaves_groups(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    gs.suspend("ann@example.org", actor=ADMIN)
    assert not gs.can("ann@example.org", HUB, "crm_read")
    assert gs.permissions_for("ann@example.org", HUB) == set()
    # Blocking removes access held through groups like direct grants:
    # reactivating restores none of it.
    assert gs.groups_for("ann@example.org") == []
    gs.suspend("ann@example.org", actor=ADMIN, suspended=False)
    assert not gs.can("ann@example.org", HUB, "crm_read")
    assert gs.can("bob@example.org", HUB, "crm_read")


def test_an_unavailable_account_holds_nothing_through_groups(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    # The marker the account sync writes for a removed or pending account.
    with gs.engine.begin() as conn:
        gs._meta_set(conn, "account_unavailable:bob@example.org", "1")
    assert not gs.can("bob@example.org", HUB, "crm_read")


def test_public_grant_and_direct_grants_keep_working(gs):
    g = _group(gs, members=("ann@example.org",))
    gs.grant(EVERYONE, HUB, USE_HUB, actor=ADMIN, carry_over_public=True)
    gs.grant("carl@example.org", HUB, "crm_read", actor=ADMIN)
    assert gs.can("zed@example.org", HUB, USE_HUB)          # everyone signed in
    assert gs.can("carl@example.org", HUB, "crm_read")      # direct
    assert not gs.can("ann@example.org", HUB, "crm_read")   # the group holds nothing yet
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    assert gs.can("ann@example.org", HUB, "crm_read")


def test_groups_never_hold_administration(gs):
    g = _group(gs)
    subject = group_subject(g["id"])
    for hub, perm in ((HUB, MANAGE_ACCESS), (ORG, MANAGE_ACCESS)):
        with pytest.raises(ValueError):
            gs.grant(subject, hub, perm, actor=ADMIN)
    # Even a row written behind the store's back never makes a member an admin.
    with gs.engine.begin() as conn:
        conn.execute(text("INSERT INTO hz_grants (subject, hub, permission, created) "
                          "VALUES (:s, :h, :p, :t)"),
                     {"s": subject, "h": ORG, "p": MANAGE_ACCESS, "t": time.time()})
        conn.execute(text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id=1"))
    assert not gs.can("ann@example.org", ORG, MANAGE_ACCESS)
    assert not gs.can("ann@example.org", HUB, MANAGE_ACCESS)
    assert MANAGE_ACCESS not in gs.permissions_for("ann@example.org", HUB)


def test_a_grant_to_a_missing_group_is_refused(gs):
    with pytest.raises(GroupNotFound):
        gs.grant("group:g_nope", HUB, USE_HUB, actor=ADMIN)
    with pytest.raises(ValueError):
        gs.grant("group:not a valid id", HUB, USE_HUB, actor=ADMIN)
    assert not any(is_group_subject(s) for s, _h, _p in gs.list_grants())


def test_deleting_a_group_removes_its_grants_and_members(gs):
    g = _group(gs)
    subject = group_subject(g["id"])
    gs.grant(subject, HUB, "crm_read", actor=ADMIN)
    gs.grant(subject, "support", USE_HUB, actor=ADMIN)
    result = gs.delete_group(g["id"], actor=ADMIN)
    assert (result["grants_removed"], result["members_removed"]) == (3, 2)
    assert not any(s == subject for s, _h, _p in gs.list_grants())
    assert not gs.can("ann@example.org", HUB, "crm_read")
    assert gs.group(g["id"]) is None and gs.groups_for("ann@example.org") == []
    with pytest.raises(GroupNotFound):
        gs.add_group_members(g["id"], ["x@example.org"], actor=ADMIN)
    actions = [(a["action"], a["subject"]) for a in gs.read_access_audit(50)]
    assert ("group_delete", subject) in actions
    assert sum(1 for a, s in actions if a == "revoke" and s == subject) == 3


def test_groups_are_not_people(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, USE_HUB, actor=ADMIN)
    subjects = {i["subject"] for i in gs.identities()}
    assert group_subject(g["id"]) not in subjects
    # Members are listed like any grantee (awaiting sign-up until they sign in).
    assert {"ann@example.org", "bob@example.org"} <= subjects
    with pytest.raises(ValueError):
        gs.suspend(group_subject(g["id"]), actor=ADMIN)


def test_members_must_be_email_addresses(gs):
    for bad in (["not-an-email"], ["group:g_x"], [EVERYONE], [""]):
        with pytest.raises(GroupError):
            gs.create_group("Bad", actor=ADMIN, emails=bad)


def test_held_since_follows_group_membership(gs):
    g = _group(gs, members=("ann@example.org",))
    gs.grant(group_subject(g["id"]), HUB, "share_public_links", actor=ADMIN)
    first = gs.held_since("ann@example.org", HUB, "share_public_links")
    assert first is not None
    time.sleep(0.01)
    gs.remove_group_member(g["id"], "ann@example.org", actor=ADMIN)
    assert gs.held_since("ann@example.org", HUB, "share_public_links") is None
    gs.add_group_members(g["id"], ["ann@example.org"], actor=ADMIN)
    # Leaving and rejoining is a break: the hold starts again.
    assert gs.held_since("ann@example.org", HUB, "share_public_links") > first


def test_leaving_everything_on_account_replacement_and_revoke_all(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, USE_HUB, actor=ADMIN)
    gs.upsert_identity(email="ann@example.org", owui_id="u-1")
    gs.upsert_identity(email="ann@example.org", owui_id="u-2")   # a new account, same email
    assert gs.groups_for("ann@example.org") == []
    gs.revoke_all("bob@example.org", actor=ADMIN)
    assert gs.groups_for("bob@example.org") == []
    assert not gs.can("bob@example.org", HUB, USE_HUB)


def test_rename_and_describe_are_audited(gs):
    g = _group(gs)
    rev = gs.revision()
    out = gs.update_group(g["id"], name="Revenue", description="Deals and renewals", actor=ADMIN)
    assert (out["name"], out["description"]) == ("Revenue", "Deals and renewals")
    assert gs.revision() > rev
    out = gs.update_group(g["id"], description=None, actor=ADMIN)
    assert out["description"] is None and out["name"] == "Revenue"
    actions = [a["action"] for a in gs.read_access_audit(50)]
    assert "group_rename" in actions and "group_describe" in actions


def test_snapshot_with_groups_is_consistent(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    rev, grants, groups = gs.access_snapshot_with_groups()
    assert rev == gs.revision()
    assert (group_subject(g["id"]), HUB, "crm_read") in grants
    assert groups[g["id"]] == {"name": "Sales team", "members": {"ann@example.org", "bob@example.org"}}
    assert gs.access_snapshot() == (rev, grants)


def test_restore_skips_grants_for_deleted_groups(gs):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    snap = gs.snapshot([HUB])
    gs.delete_group(g["id"], actor=ADMIN)
    gs.restore(snap, actor="cli:test")
    assert not any(is_group_subject(s) for s, _h, _p in gs.list_grants())
    assert any(a["action"] == "restore_skipped" for a in gs.read_access_audit(50))


def test_a_blocked_decision_path_fails_closed_when_memberships_cannot_be_read(gs, monkeypatch):
    g = _group(gs)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)

    def broken():
        raise RuntimeError("database gone")

    monkeypatch.setattr(gs, "_load_members", broken)
    with gs.engine.begin() as conn:  # force a reload
        conn.execute(text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id=1"))
    with pytest.raises(RuntimeError):
        gs.can("ann@example.org", HUB, "crm_read")


# ---------------------------------------------------------------------------
# PostgreSQL (ephemeral, local binaries only; skipped without them)
# ---------------------------------------------------------------------------
@pytest.fixture()
def pg_store(postgres_url):
    """A GrantStore on its own fresh PostgreSQL database."""
    import secrets as _secrets

    from sqlalchemy.engine import make_url

    name = "hz_groups_" + _secrets.token_hex(4)
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(postgres_url).set(database=name)
    engines = [create_engine(url), create_engine(url)]
    try:
        yield [GrantStore(e) for e in engines]
    finally:
        for e in engines:
            e.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_postgres_group_resolution_across_processes(pg_store):
    a, b = pg_store
    a.bootstrap([ADMIN], authoritative=True, hub=HUB)
    g = a.create_group("Sales team", actor=ADMIN, emails=["ann@example.org"])
    a.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    assert b.can("ann@example.org", HUB, "crm_read")
    assert b.permissions_for("ann@example.org", HUB) == {USE_HUB, "crm_read"}
    assert b.hubs_for("ann@example.org") == {HUB}
    assert b.held_since("ann@example.org", HUB, "crm_read") is not None
    [listed] = b.list_groups()
    assert (listed["member_count"], listed["grant_count"]) == (1, 2)
    with pytest.raises(GroupNameTaken):
        b.create_group("SALES TEAM", actor=ADMIN)
    b.remove_group_member(g["id"], "ann@example.org", actor=ADMIN)
    assert not a.can("ann@example.org", HUB, "crm_read")
    b.delete_group(g["id"], actor=ADMIN)
    assert not any(is_group_subject(s) for s, _h, _p in a.list_grants())


def test_postgres_a_grant_never_outlives_a_concurrent_delete(pg_store):
    """Deleting a group while someone gives it access: either the grant lands
    first and the delete removes it, or the delete wins and the grant is
    refused. Never a grant for a group that no longer exists."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    a, b = pg_store
    a.bootstrap([ADMIN], authoritative=True, hub=HUB)
    for _round in range(5):
        g = a.create_group(f"Race {_round}", actor=ADMIN, emails=["ann@example.org"])
        barrier = threading.Barrier(2)

        def delete():
            barrier.wait()
            a.delete_group(g["id"], actor=ADMIN)

        def grant():
            barrier.wait()
            try:
                b.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
            except GroupNotFound:
                pass

        with ThreadPoolExecutor(2) as pool:
            for f in [pool.submit(delete), pool.submit(grant)]:
                f.result(timeout=30)
        assert a.group(g["id"]) is None
        assert not any(s == group_subject(g["id"]) for s, _h, _p in a.list_grants())
        assert not a.can("ann@example.org", HUB, "crm_read")


# ---------------------------------------------------------------------------
# what a migration (lane F) relies on
# ---------------------------------------------------------------------------
def test_group_grant_support_is_detectable_and_resolves_both_ways(gs):
    from hubzoid.access import store as store_mod

    assert store_mod.SUPPORTS_GROUP_GRANTS is True
    assert GrantStore.supports_group_grants is True and gs.supports_group_grants is True
    g = gs.create_group("Night shift", actor="migration", source="migrated",
                        group_id="7C9E6679-7425-40de-944b-e07fc1f90ae7", emails=["ann@example.org"])
    assert g["id"] == "7c9e6679-7425-40de-944b-e07fc1f90ae7" and g["source"] == "migrated"
    subject = group_subject(g["id"])
    gs.apply_migration([(subject, HUB, "crm_read")], [], [HUB])
    # The group subject itself, and every member through it.
    assert gs.can(subject, HUB, "crm_read") and gs.can(subject, HUB, USE_HUB)
    assert gs.can("ann@example.org", HUB, "crm_read")
    assert not gs.can(subject, HUB, MANAGE_ACCESS)


def test_no_identity_row_for_a_group_on_any_write_path(gs):
    g = gs.create_group("Ops", actor=ADMIN)
    subject = group_subject(g["id"])
    gs.grant(subject, HUB, USE_HUB, actor=ADMIN)
    gs.grant_many([(subject, HUB, "crm_read")])
    gs.apply_migration([(subject, HUB, "crm_write")], [], [HUB], replace=False)
    gs.apply_changes(subject, HUB, [("grant", "crm_export")])
    assert gs.identity(subject) is None
    assert subject not in {i["subject"] for i in gs.identities()}


def test_memberships_written_outside_the_store_apply_once_the_revision_moves(gs):
    """A bulk import may write hz_group_members itself; like every access
    write it must bump the policy revision (apply_migration does)."""
    g = gs.create_group("Imported", actor=ADMIN)
    gs.grant(group_subject(g["id"]), HUB, "crm_read", actor=ADMIN)
    with gs.engine.begin() as conn:
        conn.execute(text("INSERT INTO hz_group_members (group_id, email, added_at) "
                          "VALUES (:g, 'zed@example.org', 0)"), {"g": g["id"]})
    assert not gs.can("zed@example.org", HUB, "crm_read")   # not until the revision moves
    with gs.engine.begin() as conn:
        conn.execute(text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id=1"))
    assert gs.can("zed@example.org", HUB, "crm_read")
