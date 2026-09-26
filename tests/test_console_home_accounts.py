"""The Console home's Users figure: `/portal/api/summary` -> `user_accounts`.

Distinct login accounts in the viewer's scope, from the chat app's user
directory. An organization administrator sees the deployment; a delegate sees
only accounts holding access in the hubs they manage. The figure does not
follow the period, and an unreadable directory is unavailable, never 0. The
period-bound `active_users` keeps its meaning.

No model and no network: the two-hub review deployment from
`tests.review_fixtures`, with an Open WebUI user table in its SQLite file.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from hubzoid import db, deployment
from hubzoid.portal import PortalAdmin, build_router

from tests.review_fixtures import OPERATIONS, OWNER, REVIEW, make_review_deployment

LEAD = "lead@example.org"  # manages review-hub only


def _owui_db(dep) -> Path:
    return Path(deployment.read(dep.hubs[REVIEW])["owui_db"])


def _seed_accounts(dep, *emails_roles) -> None:
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
def home(tmp_path, monkeypatch):
    """ann (both hubs), bob (review), cara (operations), dan (blocked), a pending
    login with no grant, the review-hub delegate, the owner; plus an email-only
    grant and a service identity, neither of which is an account."""
    dep = make_review_deployment(tmp_path, monkeypatch)
    gs = dep.gs
    gs.grant("ann@example.org", REVIEW, "sample_inventory", actor="test")
    gs.grant("ann@example.org", OPERATIONS, "use_hub", actor="test")
    gs.grant("bob@example.org", REVIEW, "use_hub", actor="test")
    gs.grant("cara@example.org", OPERATIONS, "sample_incidents", actor="test")
    gs.grant("dan@example.org", REVIEW, "use_hub", actor="test")
    gs.suspend("dan@example.org", actor="test")
    gs.grant(LEAD, REVIEW, "manage_access", actor="test")
    gs.grant("pre@example.org", REVIEW, "use_hub", actor="test")
    gs.grant("workflow:nightly", REVIEW, "sample_reports", actor="test")
    _seed_accounts(dep, (OWNER, "admin"), ("ann@example.org", "user"), ("bob@example.org", "user"),
                   ("cara@example.org", "user"), ("dan@example.org", "user"),
                   ("waiting@example.org", "pending"), (LEAD, "user"))
    who = {"admin": PortalAdmin(subject=OWNER, is_org_admin=True, manageable=[])}
    app = FastAPI()
    app.include_router(build_router(dep.hubs[REVIEW], admin_resolver=lambda _r: who["admin"]))
    client = TestClient(app)
    client.dep, client.who = dep, who  # type: ignore[attr-defined]
    return client


def _as_delegate(client, hubs=(REVIEW,)):
    client.who["admin"] = PortalAdmin(subject=LEAD, is_org_admin=False, manageable=list(hubs))


def _summary(client, period="7d") -> dict:
    r = client.get("/portal/api/summary", params={"period": period})
    assert r.status_code == 200, r.text
    return r.json()


def test_an_organization_administrator_sees_every_login_account(home):
    # owner, ann, bob, cara, dan (blocked), waiting (pending), lead.
    assert _summary(home)["user_accounts"] == {"accounts": 7, "hubs": 2}


def test_a_delegate_sees_only_accounts_in_their_hubs(home):
    _as_delegate(home)
    body = _summary(home)
    # owner (entry at setup), ann, bob, lead. Never cara (operations-hub only),
    # and the hub count names only the delegate's own hub.
    assert body["user_accounts"] == {"accounts": 4, "hubs": 1}
    assert [h["key"] for h in body["hubs"]] == [REVIEW]


def test_a_delegate_with_no_hub_counts_nothing_from_other_hubs(home):
    _as_delegate(home, hubs=())
    assert _summary(home)["user_accounts"] == {"accounts": 0, "hubs": 0}


def test_the_account_figure_does_not_follow_the_period(home):
    eng = db.operational_engine(home.dep.hubs[REVIEW])
    now = time.time()
    with eng.begin() as c:
        for ts, who in ((now - 3600, "ann@example.org"), (now - 3 * 86400, "bob@example.org")):
            c.execute(text(
                "INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, status) "
                "VALUES (:ts, :hub, 'web', 'chat', :s, :c, 'm', 'ok')"),
                {"ts": ts, "hub": REVIEW, "s": who, "c": "c-" + who})
    day, week, month = (_summary(home, p) for p in ("24h", "7d", "30d"))
    assert day["user_accounts"] == week["user_accounts"] == month["user_accounts"]
    # The period-bound figure keeps its meaning: people who sent a message.
    assert (day["totals"]["active_users"], week["totals"]["active_users"]) == (1, 2)


def test_an_unreadable_directory_is_unavailable_not_zero(home):
    _owui_db(home.dep).unlink()
    assert _summary(home)["user_accounts"] == {"accounts": None, "hubs": 2}
    _as_delegate(home)
    assert _summary(home)["user_accounts"] == {"accounts": None, "hubs": 1}


def test_a_failing_count_never_breaks_the_home(home, monkeypatch):
    import hubzoid.access.account_counts as counts

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(counts, "user_account_summary", boom)
    body = _summary(home)
    assert body["user_accounts"] == {"accounts": None, "hubs": 2}
    assert body["totals"]["messages"] == 0
