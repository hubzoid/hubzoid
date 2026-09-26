"""Change requests: proposed by an agent tool or API, applied only when the same
person confirms the exact plan in the Console with a verified web session.

Covers expiry, replay, wrong actor, plan-hash mismatch, a ceiling that shrank
since the proposal, the 20-request cap, and the audit trail (surface and
request id on every row). No model and no network.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from hubzoid.access.service import MAX_PENDING_REQUESTS, Denied

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    actor,
    dep,
)

PASSWORD = "Correct-Horse-7-battery"


def _row(dep, rid):
    with dep.gs.engine.connect() as c:
        return c.execute(text("SELECT * FROM hz_change_requests WHERE id=:id"),
                         {"id": rid}).mappings().fetchone()


def _propose_access(dep, who=DELEGATE, surface="whatsapp", **plan):
    base = dict(kind="access", hub="finance", subject="ann@x.org", grant=["ledger"])
    base.update(plan)
    return dep.svc.propose(actor(who, surface=surface, via="bridge"), base)


def _hash(dep, who, rid):
    return dep.svc.get_request(actor(who), rid)["plan_hash"]


def test_propose_then_confirm_applies_exactly_once(dep):
    proposed = _propose_access(dep)
    rid = proposed["id"]
    assert proposed["confirm_path"] == f"/portal/#/confirm/{rid}"
    assert "ledger" in proposed["summary"].lower() or "Ledger" in proposed["summary"]
    assert not dep.gs.can("ann@x.org", "finance", "ledger")  # nothing yet
    row = _row(dep, rid)
    assert row["status"] == "pending" and row["surface"] == "whatsapp" and row["actor"] == DELEGATE
    view = dep.svc.get_request(actor(DELEGATE), rid)
    assert view["plan"] == {"kind": "access", "hub": "finance", "subject": "ann@x.org",
                            "grant": ["ledger"], "revoke": []}
    assert view["problem"] is None
    out = dep.svc.confirm(actor(DELEGATE), rid, plan_hash=view["plan_hash"])
    assert out["status"] == "confirmed"
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    assert _row(dep, rid)["status"] == "confirmed"
    # Replay is refused and changes nothing.
    rev = dep.gs.revision()
    with pytest.raises(Denied) as e:
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=view["plan_hash"])
    assert e.value.code == "not_pending" and dep.gs.revision() == rev
    # The audit trail links every step to the request and where it came from.
    rows = dep.gs.read_access_audit(20, request_id=rid)
    actions = sorted(r["action"] for r in rows)
    assert actions == ["change_confirmed", "change_proposed", "grant", "grant"]
    grants = [r for r in rows if r["action"] == "grant"]
    assert all(r["surface"] == "whatsapp" and r["actor"] == DELEGATE for r in grants)
    assert next(r for r in rows if r["action"] == "change_confirmed")["surface"] == "console"


def test_wrong_actor_cannot_see_or_confirm(dep):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    for who in (ROOT, "ann@x.org"):
        with pytest.raises(Denied) as e:
            dep.svc.get_request(actor(who), rid)
        assert e.value.status == 404
        with pytest.raises(Denied) as e:
            dep.svc.confirm(actor(who), rid, plan_hash=good)
        assert e.value.status == 404
        with pytest.raises(Denied):
            dep.svc.reject(actor(who), rid)
    assert _row(dep, rid)["status"] == "pending"
    with pytest.raises(Denied) as e:
        dep.svc.get_request(actor(DELEGATE), "not a valid id!")
    assert e.value.status == 404


def test_confirmation_needs_a_web_session(dep):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    for via in ("bridge", "api-key"):
        with pytest.raises(Denied) as e:
            dep.svc.confirm(actor(DELEGATE, surface="api", via=via), rid, plan_hash=good)
        assert e.value.code == "session_required"
    assert _row(dep, rid)["status"] == "pending"


def test_plan_hash_must_match(dep):
    rid = _propose_access(dep)["id"]
    with pytest.raises(Denied) as e:
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash="0" * 64)
    assert e.value.code == "plan_changed"
    assert _row(dep, rid)["status"] == "pending"
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_expired_request_is_refused_and_audited(dep, monkeypatch):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    with dep.gs.engine.begin() as c:
        c.execute(text("UPDATE hz_change_requests SET expires=:t WHERE id=:id"),
                  {"t": time.time() - 1, "id": rid})
    with pytest.raises(Denied) as e:
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=good)
    assert e.value.status == 410
    assert _row(dep, rid)["status"] == "expired"
    assert "change_expired" in [r["action"] for r in dep.gs.read_access_audit(10, request_id=rid)]
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_ttl_from_environment(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_CHANGE_REQUEST_TTL", "120")
    proposed = _propose_access(dep)
    assert 100 < proposed["expires"] - time.time() <= 120


def test_ceiling_shrunk_before_confirm(dep):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    dep.gs.revoke(DELEGATE, "finance", "ledger", actor=ROOT)
    assert dep.svc.get_request(actor(DELEGATE), rid)["problem"]
    with pytest.raises(Denied) as e:
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=good)
    assert e.value.code == "outside_ceiling"
    assert _row(dep, rid)["status"] == "failed"
    assert not dep.gs.can("ann@x.org", "finance", "ledger")
    assert "change_failed" in [r["action"] for r in dep.gs.read_access_audit(10, request_id=rid)]


def test_delegate_losing_management_cannot_confirm(dep):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    dep.gs.revoke(DELEGATE, "finance", "manage_access", actor=ROOT)
    with pytest.raises(Denied):
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=good)
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_reject(dep):
    rid = _propose_access(dep)["id"]
    good = _hash(dep, DELEGATE, rid)
    dep.svc.reject(actor(DELEGATE), rid)
    assert _row(dep, rid)["status"] == "rejected"
    with pytest.raises(Denied):
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=good)
    with pytest.raises(Denied):
        dep.svc.reject(actor(DELEGATE), rid)
    assert "change_rejected" in [r["action"] for r in dep.gs.read_access_audit(10, request_id=rid)]


def test_proposals_are_checked_up_front(dep):
    for plan in (dict(grant=["payroll"]), dict(grant=["manage_access"]), dict(hub="ops"),
                 dict(subject=DELEGATE), dict(grant=[], revoke=[]),
                 dict(grant=["ledger"], revoke=["ledger"]), dict(kind="other")):
        with pytest.raises(Denied):
            _propose_access(dep, **plan)
    with pytest.raises(Denied):
        _propose_access(dep, who="nobody@x.org")
    with dep.gs.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM hz_change_requests")).scalar() == 0


def test_pending_cap(dep):
    for i in range(MAX_PENDING_REQUESTS):
        _propose_access(dep, subject=f"p{i}@x.org")
    with pytest.raises(Denied) as e:
        _propose_access(dep, subject="one-more@x.org")
    assert e.value.status == 429
    # Another manager has their own allowance.
    _propose_access(dep, who=ROOT, subject="one-more@x.org")


def test_account_proposal_never_holds_a_password(dep):
    proposed = dep.svc.propose(actor(DELEGATE, surface="mcp", via="bridge"), dict(
        kind="account", hub="finance", email="New@X.org", name="New Person", grant=["ledger"]))
    rid = proposed["id"]
    view = dep.svc.get_request(actor(DELEGATE), rid)
    assert view["plan"] == {"kind": "account", "hub": "finance", "email": "new@x.org",
                            "name": "New Person", "grant": ["ledger", "use_hub"]}
    with pytest.raises(Denied) as e:  # a password is set at confirmation
        dep.svc.confirm(actor(DELEGATE), rid, plan_hash=view["plan_hash"])
    assert e.value.code == "invalid_password"
    assert _row(dep, rid)["status"] == "pending"
    out = dep.svc.confirm(actor(DELEGATE), rid, plan_hash=view["plan_hash"], password=PASSWORD)
    assert out["result"]["subject"] == "new@x.org"
    assert dep.owui.by_email("new@x.org")["role"] == "user"
    assert dep.gs.can("new@x.org", "finance", "ledger")
    stored = json.dumps([dict(r) for r in [_row(dep, rid)]], default=str)
    stored += json.dumps(dep.gs.read_access_audit(50), default=str)
    assert PASSWORD not in stored and PASSWORD not in json.dumps(out)
    rows = dep.gs.read_access_audit(20, request_id=rid)
    create = next(r for r in rows if r["action"] == "account_create")
    assert create["surface"] == "mcp"


def test_account_confirmation_survives_a_rejected_password(dep):
    rid = dep.svc.propose(actor(ROOT, surface="owui", via="bridge"), dict(
        kind="account", hub="ops", email="n@x.org", name="N"))["id"]
    good = _hash(dep, ROOT, rid)
    dep.owui.reject_password = True
    with pytest.raises(Denied) as e:
        dep.svc.confirm(actor(ROOT), rid, plan_hash=good, password=PASSWORD)
    assert e.value.code == "rejected"
    assert _row(dep, rid)["status"] == "pending"  # nothing applied, still usable
    dep.owui.reject_password = False
    dep.svc.confirm(actor(ROOT), rid, plan_hash=good, password=PASSWORD)
    assert _row(dep, rid)["status"] == "confirmed"


def test_account_proposal_for_existing_person_is_refused(dep):
    dep.gs.upsert_identity(email="ann@x.org", owui_id="u-ann")
    with pytest.raises(Denied) as e:
        dep.svc.propose(actor(ROOT, surface="owui", via="bridge"), dict(
            kind="account", hub="finance", email="ann@x.org", name="Ann"))
    assert e.value.code == "account_exists"
