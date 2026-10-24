"""An open chat event stream ends at the next delivery once the person's
access ends: signed out everywhere, blocked, or agent access removed. Real
sessions (sign-in on), not the test identity header."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, build_app, make_hub

PASSWORD = "a long test password 1"


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch, HUBZOID_AUTH="true", HUBZOID_MFA_REQUIRED="none")
    from hubzoid.chat import engine_routes

    monkeypatch.setattr(engine_routes, "KEEPALIVE_SECONDS", 0.2)
    from hubzoid.auth import users

    ana = users.create(hub, email="ana@example.org", name="Ana", password=PASSWORD)
    users.sync_identity(hub, ana)
    # One event loop for the whole test: without `with`, a reply's run started
    # by one request could be dropped when that request returned (release CI).
    with TestClient(build_app()) as client:
        r = client.post("/api/auth/login", json={"email": ana["email"], "password": PASSWORD},
                        headers=ORIGIN)
        assert r.status_code == 200, r.text
        r = client.post("/api/conversations", headers=ORIGIN,
                        json={"agent": client.app.state.chat.model_label})
        assert r.status_code == 201, r.text
        cid = r.json()["conversation"]["id"]
        yield hub, client, ana, cid


def _until_revoked(client, cid, end_access) -> list[str]:
    seen: list[str] = []
    with client.stream("GET", f"/api/conversations/{cid}/events?after=0") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            seen.append(line)
            if line.startswith("retry:"):
                end_access()
            if "access.revoked" in line or len(seen) > 200:
                break
    return seen


@pytest.mark.parametrize("how", ["sign_out_everywhere", "block", "remove_agent_access"])
def test_the_stream_stops_when_access_ends(signed_in, how):
    hub, client, ana, cid = signed_in
    from hubzoid.access import store_for
    from hubzoid.auth import users

    def end_access():
        if how == "sign_out_everywhere":
            users.revoke_credentials(hub, ana["id"])
        elif how == "block":
            store_for(hub).suspend("ana@example.org", actor="test")
        else:
            store_for(hub).revoke("ana@example.org", hub.name, "use_hub", actor="test")

    seen = _until_revoked(client, cid, end_access)
    assert any("access.revoked" in line for line in seen), seen[-5:]


def _reply(client, cid, headers):
    import uuid

    from tests.test_chat_operations import wait_run

    branch = client.get(f"/api/conversations/{cid}", headers=headers).json()["branches"][0]
    r = client.post(f"/api/conversations/{cid}/operations",
                    headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                    json={"kind": "send", "branch_id": branch["id"],
                          "expected_revision": branch["revision"], "parent_id": branch.get("head_id"),
                          "content": [{"type": "text", "text": "hello"}]})
    assert r.status_code == 202, r.text
    wait_run(client, r.json()["run_id"], headers)


def test_a_web_reply_carries_its_session_and_a_key_reply_its_key(signed_in):
    """F2: the reply's run is tied to the device or key that sent it, so
    revoking that one stops its next tool call; a key-sent reply is an API run
    with no one watching."""
    hub, client, ana, cid = signed_in
    from hubzoid.access import evaluator, run_identity
    from hubzoid.auth import api_keys, sessions
    from hubzoid.auth.schema import engine_for
    import sqlalchemy as sa

    def latest(kind):
        with engine_for(hub).connect() as conn:
            row = conn.execute(sa.select(run_identity.records).where(
                run_identity.records.c.run_kind == kind).order_by(
                run_identity.records.c.created_at.desc())).first()
        return run_identity._row(row)

    _reply(client, cid, ORIGIN)
    web = latest("chat")
    assert web["credential_kind"] == "session" and web["credential_id"] and web["person_present"]
    ident = run_identity.identity(web, "web")
    assert evaluator.check_run(hub, ident)
    assert sessions.revoke_session_id(hub, ana["id"], web["credential_id"])
    assert evaluator.check_run(hub, ident).reason == "credential_ended"

    user = sessions.AuthUser(id=ana["id"], email=ana["email"], name="Ana", role="user")
    _, key = api_keys.create(hub, user, name="script", operations=["chat"])
    script = TestClient(client.app)
    _reply(script, cid, {"authorization": f"Bearer {key}"})
    api = latest("api")
    assert api["credential_kind"] == "api_key" and not api["person_present"]


def test_no_agent_access_means_no_replay_either(signed_in):
    """Reconnecting with after=0 replays nothing to someone whose agent
    access was removed (Codex review)."""
    hub, client, ana, cid = signed_in
    from hubzoid.access import store_for

    _reply(client, cid, ORIGIN)
    store_for(hub).revoke("ana@example.org", hub.name, "use_hub", actor="test")
    r = client.get(f"/api/conversations/{cid}/events?after=0")
    assert r.status_code == 403 and "run.changed" not in r.text


def test_a_store_outage_closes_the_stream_without_revoking(signed_in, monkeypatch):
    hub, client, ana, cid = signed_in
    from fastapi import HTTPException
    from hubzoid.chat import common

    def outage():
        def down(*a, **k):
            raise HTTPException(status_code=503, detail="unavailable")
        monkeypatch.setattr(common, "check_use_hub", down)

    seen = _until_revoked(client, cid, outage)
    assert not any("access.revoked" in line for line in seen)


def test_queued_and_resumed_work_needs_its_sign_in_still_valid(signed_in):
    """A queued message or a reply resumed after an approval keeps the
    session or key it came with: revoked (or signed out everywhere) since,
    it doesn't start. A resumed reply survives plain session expiry."""
    hub, client, ana, cid = signed_in
    from hubzoid.access.evaluator import still_valid
    from hubzoid.auth import AuthUser, api_keys, sessions, users

    _, key = api_keys.create(hub, AuthUser(id=ana["id"], email=ana["email"], name="Ana", role="user"),
                             name="q", operations=["chat"])
    kid = api_keys.list_for(hub, ana["id"])[0]["id"]
    cred = {"kind": "api_key", "id": kid, "epoch": users.epoch(hub, ana["id"])}
    assert still_valid(hub, ana["id"], cred)
    api_keys.revoke(hub, ana["id"], kid)
    assert not still_valid(hub, ana["id"], cred)
    sid = [s for s in sessions.list_sessions(hub, ana["id"], "")][0]["id"]
    cred = {"kind": "session", "id": sid, "epoch": users.epoch(hub, ana["id"])}
    assert still_valid(hub, ana["id"], cred)
    users.revoke_credentials(hub, ana["id"])          # signed out everywhere
    assert not still_valid(hub, ana["id"], cred, expired_ok=True)
