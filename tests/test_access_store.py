"""Unit tests for the Casbin-backed access store (direct grants only).

Pure: SQLite + casbin, no LLM, no network, no Open WebUI.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from hubzoid.access.store import (
    EVERYONE,
    LastAdminError,
    MANAGE_ACCESS,
    ORG,
    USE_HUB,
    GrantStore,
)


@pytest.fixture()
def store(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    return GrantStore(eng)


def test_grant_implies_use_hub(store):
    store.grant("alice", "finance", "prod_in")
    assert store.can("alice", "finance", "prod_in")
    assert store.can("alice", "finance", USE_HUB)  # implied
    # not in another hub
    assert not store.can("alice", "ops", "prod_in")
    assert not store.can("alice", "ops", USE_HUB)


def test_direct_agent_admin_includes_entry_but_not_restricted_tools(store):
    store.grant("alice", "finance", MANAGE_ACCESS)
    assert store.can("alice", "finance", MANAGE_ACCESS)
    assert store.can("alice", "finance", USE_HUB)
    assert not store.can("alice", "finance", "read_reports")
    assert not store.can("alice", "ops", MANAGE_ACCESS)
    assert not store.can("alice", "ops", USE_HUB)


def test_revoke_tool_keeps_use_hub(store):
    store.grant("alice", "finance", "prod_in")
    store.revoke("alice", "finance", "prod_in")
    assert not store.can("alice", "finance", "prod_in")
    assert store.can("alice", "finance", USE_HUB)  # still in the hub


def test_revoke_use_hub_cascades(store):
    store.grant("alice", "finance", "prod_in")
    store.grant("alice", "finance", "read_reports")
    store.revoke("alice", "finance", USE_HUB)
    assert not store.can("alice", "finance", USE_HUB)
    assert not store.can("alice", "finance", "prod_in")
    assert not store.can("alice", "finance", "read_reports")
    assert store.list_grants("finance") == []


def test_wildcard_subject_public_use_hub(store):
    store.grant(EVERYONE, "public", USE_HUB, carry_over_public=True)
    assert store.can("anyone-at-all", "public", USE_HUB)
    assert store.can("someone-else", "public", USE_HUB)
    assert not store.can("anyone-at-all", "public", "prod_in")


def test_org_admin_spans_all_hubs(store):
    store.grant("root", ORG, MANAGE_ACCESS)
    assert store.can("root", "finance", MANAGE_ACCESS)
    assert store.can("root", "ops", MANAGE_ACCESS)
    assert store.can("root", "any-new-hub", MANAGE_ACCESS)
    # Organization-wide administration does not imply hub entry or tools.
    assert not store.can("root", "finance", USE_HUB)
    assert not store.can("root", "finance", "prod_in")


def test_last_org_admin_cannot_be_removed(store):
    store.grant("root", ORG, MANAGE_ACCESS)
    with pytest.raises(LastAdminError):
        store.revoke("root", ORG, MANAGE_ACCESS)
    assert store.can("root", "finance", MANAGE_ACCESS)


def test_second_admin_lets_first_be_removed(store):
    store.grant("root", ORG, MANAGE_ACCESS)
    store.grant("root2", ORG, MANAGE_ACCESS)
    store.revoke("root", ORG, MANAGE_ACCESS)  # now allowed
    assert not store.can("root", "finance", MANAGE_ACCESS)
    assert store.can("root2", "finance", MANAGE_ACCESS)


def test_revoke_all_respects_last_admin(store):
    store.grant("root", ORG, MANAGE_ACCESS)
    with pytest.raises(LastAdminError):
        store.revoke_all("root")
    store.grant("alice", "finance", "prod_in")
    store.revoke_all("alice")
    assert store.list_grants("finance") == []


def test_permissions_for_and_hubs_for(store):
    store.grant("alice", "finance", "prod_in")
    store.grant("alice", "ops", "read_reports")
    store.grant(EVERYONE, "public", USE_HUB, carry_over_public=True)
    assert store.permissions_for("alice", "finance") == {"prod_in", USE_HUB}
    # alice's own hubs plus the public one (the wildcard use_hub applies to her too)
    assert store.hubs_for("alice") == {"finance", "ops", "public"}
    # the public wildcard shows up for any subject's hub list
    assert store.hubs_for("bob") == {"public"}


def test_grant_many_bulk_import(store):
    store.grant_many([
        ("alice", "finance", "prod_in"),
        ("bob", "finance", "read_reports"),
        ("alice", "ops", "read_reports"),
    ])
    assert store.can("alice", "finance", "prod_in")
    assert store.can("bob", "finance", USE_HUB)
    assert store.can("alice", "ops", "read_reports")


def test_cross_process_freshness(tmp_path):
    url = f"sqlite:///{tmp_path / 'hub.db'}"
    writer = GrantStore(create_engine(url))
    reader = GrantStore(create_engine(url))  # a second "process"
    assert not reader.can("alice", "finance", "prod_in")
    writer.grant("alice", "finance", "prod_in")
    # reader must pick up the write via the revision bump
    assert reader.can("alice", "finance", "prod_in")


def test_normalization(store):
    store.grant("Alice", "Finance", "Prod_In")
    assert store.can("Alice", "finance", "prod_in")
    assert store.can("Alice", " FINANCE ".strip(), "prod_in")


def test_subject_case_insensitive(store):
    # a grant made with mixed case matches the lowercased login identity
    store.grant("Alice@Corp", "finance", "prod_in")
    assert store.can("alice@corp", "finance", "prod_in")
    assert store.can("ALICE@CORP", "finance", "use_hub")
    assert store.permissions_for("alice@corp", "finance") == {"prod_in", USE_HUB}


def test_access_audit_records_changes(store):
    store.grant("alice", "finance", "prod_in", actor="root")
    store.revoke("alice", "finance", "prod_in", actor="root")
    rows = store.read_access_audit()
    actions = [(r["action"], r["actor"], r["subject"]) for r in rows]
    assert ("revoke", "root", "alice") in actions
    assert ("grant", "root", "alice") in actions
    # newest first
    assert rows[0]["action"] == "revoke"


def test_wildcard_permission_rejected(store):
    with pytest.raises(ValueError):
        store.grant("alice", "finance", "*")   # would match every action incl manage_access
    # Reject the entire batch instead of silently applying a partial import.
    with pytest.raises(ValueError):
        store.grant_many([("bob", "finance", "*"), ("bob", "finance", "prod_in")])
    assert not store.can("bob", "finance", "prod_in")
    assert not store.can("bob", "finance", "manage_access")


def test_per_hub_authority_isolation(store):
    store.set_authoritative(True, hub="finance")
    assert store.is_authoritative("finance") is True
    assert store.is_authoritative("ops") is False       # a different hub stays legacy
    assert store.is_authoritative() is False            # no deployment-global marker


def test_authoritative_marker(store):
    assert store.is_authoritative() is False
    store.set_authoritative(True)
    assert store.is_authoritative() is True             # deployment-global
    assert store.is_authoritative("anyhub") is True     # global covers all hubs
    store.set_authoritative(False)
    assert store.is_authoritative() is False


def test_upsert_identity(store):
    subj = store.upsert_identity(email="Alice@Corp", owui_id="u1", display="Alice")
    assert subj == "alice@corp"                      # subject = normalized email
    row = store.identity(subj)
    assert row["email"] == "alice@corp" and row["owui_id"] == "u1"
    # idempotent + fills missing fields without clobbering
    store.upsert_identity(email="alice@corp", phone="+15551234")
    row = store.identity(subj)
    assert row["owui_id"] == "u1" and row["phone"] == "+15551234"


def test_bootstrap_grants_org_admins_once(store):
    store.bootstrap(["root", "ops"], authoritative=True)
    assert store.can("root", "any-hub", MANAGE_ACCESS)
    assert store.can("ops", "any-hub", MANAGE_ACCESS)
    assert store.is_authoritative() is True
    # idempotent: a second call with different admins does nothing (already done)
    store.bootstrap(["someone-else"])
    assert not store.can("someone-else", "any-hub", MANAGE_ACCESS)


# --- policy `can` seam (surface gate stays in front) -------------------------
from hubzoid.access.identity import Identity  # noqa: E402
from hubzoid.access.policy import is_allowed  # noqa: E402


def test_policy_can_seam_gates_after_surface():
    ident = Identity.make("alice@corp", groups=[], surface="owui")
    # grant present -> allowed
    ok, reason = is_allowed(ident, "prod_in", can=lambda: True)
    assert ok and reason == "grant"
    # grant absent -> denied
    ok, reason = is_allowed(ident, "prod_in", can=lambda: False)
    assert not ok and reason == "no-grant"


def test_policy_surface_gate_beats_grant():
    # a Slack-channel caller is denied BEFORE can() is ever consulted
    ident = Identity.make("alice@corp", groups=[], surface="slack-channel")
    called = []
    ok, reason = is_allowed(
        ident, "prod_in", can=lambda: called.append(1) or True
    )
    assert not ok and reason.startswith("surface:")
    assert called == []  # can() never ran — a grant is necessary, not sufficient




def test_initial_owner_is_one_time_and_preserves_revocation(store):
    assert store.provision_owner("owner@example.com", "finance", fresh=True)
    assert store.is_authoritative("finance")
    assert store.can("owner@example.com", "finance", USE_HUB)
    store.revoke("owner@example.com", "finance", USE_HUB)
    assert not store.provision_owner("owner@example.com", "finance", fresh=True)
    assert not store.can("owner@example.com", "finance", USE_HUB)
    assert not store.provision_owner("replacement@example.com", "finance")
    assert not store.can("replacement@example.com", ORG, MANAGE_ACCESS)


def test_initial_owner_keeps_existing_hub_authority_mode(store):
    store.provision_owner("owner@example.com", "finance")
    assert not store.is_authoritative("finance")


def test_adding_hub_does_not_restore_revoked_owner_org_role(store):
    store.provision_owner("owner@example.com", "finance", fresh=True)
    store.grant("ops@example.com", ORG, MANAGE_ACCESS)
    store.revoke("owner@example.com", ORG, MANAGE_ACCESS)
    assert store.provision_owner("owner@example.com", "support", fresh=True)
    assert not store.can("owner@example.com", ORG, MANAGE_ACCESS)
    assert store.can("owner@example.com", "support", USE_HUB)


def test_owner_setup_respects_existing_admin_bootstrap(store):
    store.bootstrap(["ops@example.com"])
    store.provision_owner("owner@example.com", "finance")
    assert not store.can("owner@example.com", ORG, MANAGE_ACCESS)
