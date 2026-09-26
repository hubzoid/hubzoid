"""The Console's user-account count: scope, dedupe, exclusions and failure.

No model and no network: the two-hub review deployment from
`tests.review_fixtures`, with an Open WebUI user table written to the SQLite
file the deployment registers.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from hubzoid import deployment
import hubzoid.access as access
from hubzoid.access.account_counts import user_account_summary
from hubzoid.access.service import Scope

from tests.review_fixtures import OPERATIONS, OWNER, REVIEW, actor, make_review_deployment

LEAD = "lead@example.org"  # manages review-hub only


def _owui_db(dep) -> Path:
    return Path(deployment.read(dep.hubs[REVIEW])["owui_db"])


def seed_accounts(dep, *emails_roles) -> None:
    """Write Open WebUI's `user` table (the columns Hubzoid reads plus a few)."""
    path = _owui_db(dep)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS "user" (id TEXT PRIMARY KEY, email TEXT, '
                'name TEXT, role TEXT)')
    for email, role in emails_roles:
        con.execute('INSERT INTO "user" VALUES (?,?,?,?)',
                    (str(uuid.uuid4()), email, email.split("@")[0], role))
    con.commit()
    con.close()


@pytest.fixture
def dep(tmp_path, monkeypatch):
    """review-hub and operations-hub with a realistic mix of people:

    ann      login, grants in both hubs (dedupe)
    bob      login, review-hub only
    cara     login, operations-hub only
    dan      login, blocked (blocking removed his grants)
    pending  login awaiting approval, no grant yet
    lead     login, delegate for review-hub
    owner    login, organization administrator
    pre      email-only grant in review-hub, no login account
    workflow:nightly  service identity with a review-hub grant
    """
    d = make_review_deployment(tmp_path, monkeypatch)
    gs = d.gs
    gs.grant("ann@example.org", REVIEW, "sample_inventory", actor="test")
    gs.grant("ann@example.org", OPERATIONS, "use_hub", actor="test")
    gs.grant("bob@example.org", REVIEW, "use_hub", actor="test")
    gs.grant("cara@example.org", OPERATIONS, "sample_incidents", actor="test")
    gs.grant("dan@example.org", REVIEW, "use_hub", actor="test")
    gs.suspend("dan@example.org", actor="test")
    gs.grant(LEAD, REVIEW, "manage_access", actor="test")
    gs.grant("pre@example.org", REVIEW, "use_hub", actor="test")
    gs.grant("workflow:nightly", REVIEW, "sample_reports", actor="test")
    seed_accounts(d, (OWNER, "admin"), ("Ann@Example.org", "user"), ("bob@example.org", "user"),
                  ("cara@example.org", "user"), ("dan@example.org", "user"),
                  ("waiting@example.org", "pending"), (LEAD, "user"))
    return d


def _summary(dep, subject):
    return user_account_summary(dep.hubs[REVIEW], scope=dep.svc.scope(actor(subject)))


def test_an_organization_administrator_counts_every_login_account(dep):
    # owner, ann, bob, cara, dan (blocked), waiting (pending), lead.
    assert _summary(dep, OWNER) == {"accounts": 7, "hubs": 2}


def test_a_delegate_counts_only_accounts_holding_access_in_their_hubs(dep):
    # review-hub holders with a login: owner (entry at setup), ann, bob, lead.
    # Not cara (operations-hub only), dan (blocked, grants removed),
    # waiting (no grant), pre (no login), workflow:nightly (service).
    assert _summary(dep, LEAD) == {"accounts": 4, "hubs": 1}


def test_an_account_in_several_managed_hubs_counts_once(dep):
    dep.gs.grant(LEAD, OPERATIONS, "manage_access", actor="test")
    # ann holds access in both hubs; cara now joins through operations-hub.
    assert _summary(dep, LEAD) == {"accounts": 5, "hubs": 2}
    assert _summary(dep, OWNER)["accounts"] == 7  # unchanged by who manages what


def test_service_identities_and_email_only_grants_are_never_accounts(dep):
    # Even a directory row shaped like a service identity is not a person.
    seed_accounts(dep, ("workflow:nightly", "user"))
    dep.gs.grant("later@example.org", REVIEW, "sample_budget", actor="test")
    assert _summary(dep, OWNER)["accounts"] == 7
    assert _summary(dep, LEAD)["accounts"] == 4


def test_blocked_accounts_are_counted(dep):
    before = _summary(dep, OWNER)["accounts"]
    dep.gs.suspend("bob@example.org", actor="test")
    assert access.store_for(dep.hubs[REVIEW]).is_suspended("bob@example.org")
    assert _summary(dep, OWNER)["accounts"] == before


def test_public_entry_does_not_widen_a_delegates_count(dep):
    dep.gs.grant("*", REVIEW, "use_hub", actor="test", carry_over_public=True)
    assert _summary(dep, LEAD)["accounts"] == 4


def test_the_hub_count_follows_the_scope_it_is_given(dep):
    scope = Scope(org_admin=False, hubs=(OPERATIONS,))
    # owner (entry at setup), ann (both hubs) and cara hold access in operations-hub.
    assert user_account_summary(dep.hubs[REVIEW], scope=scope) == {"accounts": 3, "hubs": 1}


def test_an_unreadable_directory_is_unavailable_not_zero(dep):
    _owui_db(dep).unlink()
    assert _summary(dep, OWNER) == {"accounts": None, "hubs": 2}
    assert _summary(dep, LEAD) == {"accounts": None, "hubs": 1}


def test_a_directory_without_the_user_table_is_unavailable(dep):
    con = sqlite3.connect(_owui_db(dep))
    con.execute('DROP TABLE "user"')
    con.commit()
    con.close()
    assert _summary(dep, OWNER)["accounts"] is None


def test_an_unreadable_access_store_is_unavailable_for_a_delegate(dep, monkeypatch):
    scope = dep.svc.scope(actor(LEAD))
    gs = access.store_for(dep.hubs[REVIEW])

    def boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(gs, "list_grants", boom)
    assert user_account_summary(dep.hubs[REVIEW], scope=scope) == {"accounts": None, "hubs": 1}


def test_an_empty_directory_is_a_real_zero(dep):
    con = sqlite3.connect(_owui_db(dep))
    con.execute('DELETE FROM "user"')
    con.commit()
    con.close()
    assert _summary(dep, OWNER) == {"accounts": 0, "hubs": 2}
