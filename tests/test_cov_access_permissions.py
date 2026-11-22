"""Grant isolation properties and fail-closed account, roster and session seams."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine
from starlette.requests import Request

from hubzoid import access, auth
from hubzoid.access import accounts, evaluator, resolver, run_identity, session
from hubzoid.access.store import EVERYONE, MANAGE_ACCESS, ORG, USE_HUB, GrantStore
from hubzoid.auth import users
from tests.test_auth_mfa import hub  # noqa: F401
from tests.test_auth_routes import person


@pytest.mark.slow
@settings(max_examples=15)
@given(subject=st.sampled_from(["ana", "ben", "cy"]), hub=st.sampled_from(["sales", "ops", "docs"]),
       permission=st.sampled_from(["read_reports", "write_notes", "use_hub"]))
def test_direct_grant_is_local_and_block_stops_every_permission(subject, hub, permission):
    engine = create_engine("sqlite://")
    try:
        gs = GrantStore(engine)
        gs.grant(subject, hub, permission)
        assert gs.can(subject, hub, permission) and gs.can(subject, hub, USE_HUB)
        assert not gs.can(subject, "other", permission)
        assert not gs.can("someone-else", hub, permission)
        gs.suspend(subject, actor="test")
        assert not gs.can(subject, hub, permission) and not gs.can(subject, hub, USE_HUB)
        assert gs.permissions_for(subject, hub) == set()
    finally:
        engine.dispose()


@pytest.mark.slow
@settings(max_examples=10)
@given(subject=st.sampled_from(["ana", "ben"]), hub=st.sampled_from(["sales", "ops"]),
       source=st.sampled_from(["everyone", "organization"]))
def test_revoke_direct_keeps_independent_grant(subject, hub, source):
    engine = create_engine("sqlite://")
    try:
        gs = GrantStore(engine)
        permission = USE_HUB if source == "everyone" else MANAGE_ACCESS
        gs.grant(subject, hub, permission)
        if source == "everyone":
            gs.grant(EVERYONE, hub, USE_HUB, carry_over_public=True)
        else:
            gs.grant(subject, ORG, MANAGE_ACCESS)
        gs.revoke(subject, hub, permission)
        assert gs.can(subject, hub, permission)
        assert gs.can(subject, "other", permission) is (source == "organization")
    finally:
        engine.dispose()


def req(**headers):
    return Request({"type": "http", "method": "GET", "path": "/", "scheme": "http",
                    "server": ("testserver", 80), "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})


@pytest.mark.parametrize("kind,wanted", [("api_key", "api-key"), ("app_jwt", "app-jwt"), ("session", "session")])
def test_credential_comes_from_verified_user(hub, monkeypatch, kind, wanted):
    monkeypatch.setattr(auth, "current_user", lambda *a: SimpleNamespace(credential=kind, credential_id="id"))
    assert session.credential(req(authorization="Bearer credential"), hub) == (wanted, "id" if wanted != "session" else "")
    assert session.credential(req(), hub) == ("session", "")
    def broken(*a):
        raise RuntimeError("offline")
    monkeypatch.setattr(auth, "current_user", broken)
    assert session.credential(req(authorization="Bearer credential"), hub) == ("session", "")


@pytest.mark.parametrize("status,wanted", [(401, 401), (500, 503), (503, 503)])
def test_verification_preserves_denial_and_masks_store_failure(hub, monkeypatch, status, wanted):
    def broken(*a):
        raise HTTPException(status, "internal")
    monkeypatch.setattr(auth, "current_user", broken)
    with pytest.raises(HTTPException) as exc:
        session.verified_person(req(), hub)
    assert exc.value.status_code == wanted
    if wanted == 503:
        assert exc.value.detail == "Could not verify the account service. Try again shortly."


def test_verified_identity_recording_failure_refuses(hub, monkeypatch):
    monkeypatch.setattr(auth, "current_user", lambda *a: auth.AuthUser(id="u", email="ANA@EXAMPLE.COM", role="user"))
    def broken(*a):
        raise RuntimeError("offline")
    monkeypatch.setattr(users, "on_sign_in", broken)
    with pytest.raises(HTTPException) as exc:
        session.verified_person(req(), hub)
    assert exc.value.status_code == 503
    assert session.verified_person(req(), None) is None


def test_bearer_resolution_is_explicit(hub, monkeypatch):
    from hubzoid.auth import sessions
    person(hub)
    monkeypatch.setattr(auth, "current_user", lambda *a: None)
    monkeypatch.setattr(sessions, "resolve_token", lambda *a: auth.AuthUser(id="u", email="ana@example.com", role="user"))
    monkeypatch.setattr(users, "on_sign_in", lambda *a: None)
    assert session.verified_email(req(authorization="Bearer token"), hub) == ""
    assert session.verified_person(req(authorization="Bearer token"), hub, bearer=True) == ("u", "ana@example.com")


def test_account_directory_refusals_and_error_translation(hub, monkeypatch):
    from sqlalchemy.exc import OperationalError
    directory = accounts.HubzoidAccounts(hub)
    for fn in (directory.delete, directory.discard, directory.reset_with_link, directory.issue_link):
        with pytest.raises(accounts.AccountError) as exc:
            fn("missing")
        assert exc.value.code == "not_found"
    with pytest.raises(accounts.AccountError) as exc:
        directory.update("missing", role="user")
    assert exc.value.code == "not_found"
    with pytest.raises(ValueError, match="user role only"):
        directory.create(email="ana@example.com", name="Ana", role="admin")
    with pytest.raises(accounts.AccountError) as exc:
        directory.create(email="bad", name="Ana")
    assert (exc.value.status, exc.value.code) == (422, "rejected")
    u = directory.create(email="ana@example.com", name="Ana")
    with pytest.raises(accounts.AccountError) as exc:
        directory.create(email="ana@example.com", name="Ana")
    assert exc.value.code == "account_exists"
    with pytest.raises(accounts.AccountError) as exc:
        directory.update(u["id"], role="wrong")
    assert exc.value.code == "rejected"
    with pytest.raises(accounts.AccountError) as exc:
        directory.update(u["id"], password="short")
    assert exc.value.code == "invalid_password"
    directory.update(u["id"], role="pending", name="Updated")
    assert directory.get(u["id"])["role"] == "pending"
    assert directory.directory()[0]["display"] == "Updated"
    def broken(*a):
        raise OperationalError("select", {}, RuntimeError("offline"))
    with pytest.raises(accounts.AccountError) as exc:
        directory._run(broken)
    assert exc.value.status == 503 and not exc.value.certain
    monkeypatch.setattr(users, "store", broken)
    with pytest.raises(accounts.AccountsUnavailable) as exc:
        directory.get(u["id"])
    assert exc.value.code == "accounts_unavailable"


def test_resolver_errors_cache_bounds_and_defensive_copies(tmp_path, monkeypatch):
    path = tmp_path / "access.py"
    path.write_text('def resolve(surface, handle):\n    raise ValueError("bad")\n'
                    'def groups_for_email(email):\n    return " finance ; ; ops "\n')
    roster = resolver._FunctionRoster(path)
    assert roster("slack", "sender") is None
    assert roster.groups_for_email(None) == []
    first = roster.groups_for_email(" ANA@EXAMPLE.COM ")
    first.append("mutated")
    assert roster.groups_for_email("ana@example.com") == ["finance", "ops"]
    monkeypatch.setattr(resolver, "_PY_CACHE_MAX", 1)
    assert roster.groups_for_email("ben@example.com") == ["finance", "ops"]
    assert set(roster._cache) == {"ben@example.com"}
    def broken(*a):
        raise ValueError("offline")
    roster._groups_for_email = broken
    assert roster.groups_for_email("cy@example.com") == []
    path.write_text("nothing = 1\n")
    empty = resolver._FunctionRoster(path)
    assert empty("slack", "sender") is None and empty.groups_for_email("ana@example.com") == []
    assert resolver._copy(None) is None
    record = {"email": "a", "groups": ["one"]}
    copied = resolver._copy(record)
    copied["groups"].append("two")
    assert record["groups"] == ["one"]


def test_roster_load_and_console_store_fail_closed(hub, monkeypatch):
    resolver.reset_roster_cache()
    def broken(*a):
        raise RuntimeError("offline")
    monkeypatch.setattr(resolver, "load_resolver", broken)
    assert resolver.roster_for(hub) is None and resolver.roster_for(hub) is None
    fallback = lambda surface, handle: {"email": "fallback@example.com", "groups": []}
    roster = resolver._ConsolePhones(hub, fallback)
    monkeypatch.setattr(access, "store_for", broken)
    assert roster("whatsapp", "15550001111")["email"] == "fallback@example.com"
    resolver.reset_roster_cache()


def test_evaluator_bounded_cache_and_credential_counter(hub, monkeypatch):
    evaluator.clear_cache()
    monkeypatch.setattr(evaluator, "_CACHE_MAX", 1)
    assert evaluator._remember(("a",), evaluator.ALLOW).allowed
    deny = evaluator.Decision(False, "test")
    assert evaluator._remember(("b",), deny) == deny
    assert list(evaluator._cache) == [("b",)]
    u = person(hub)
    assert evaluator.authorize(hub, "", USE_HUB, hub=hub.name).reason == "anonymous"
    assert evaluator.authorize(hub, u["email"], USE_HUB, hub=hub.name, key_hubs=("other",)).reason == "key_scope"
    assert evaluator.still_valid(hub, u["id"], None)
    assert evaluator.still_valid(hub, u["id"], {"epoch": 0})
    users.revoke_credentials(hub, u["id"])
    assert not evaluator.still_valid(hub, u["id"], {"epoch": 0})
    assert evaluator.credential_live(hub, "legacy", "id")
    evaluator.clear_cache()


def test_run_admission_refuses_unrecorded_work(hub, monkeypatch):
    with pytest.raises(ValueError, match="unknown run kind"):
        run_identity.admit(hub, run_kind="bad", run_id="r", acts_as_email=None)
    def broken(*a, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(run_identity, "admit", broken)
    with pytest.raises(run_identity.NotAdmitted, match="could not be recorded"):
        run_identity.admit_or_refuse(hub, run_kind="chat", run_id="r", acts_as_email=None)
    assert run_identity.describe({"acts_as_email": "ana", "requested_by": "ben", "started_from": "api"}) == \
        "Runs as ana, requested by ben through the API (no one watching)"
