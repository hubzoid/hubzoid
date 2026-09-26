"""Restricted tools in the owner's review setup, checked without a model.

Two managed hubs share one deployment and one access store (see
`tests.review_fixtures`). Each rule is checked at the service (the Console's
`AccessService`), the guard (`visible` and the invoke wall) and the tool level
(what the model is shown and what a direct call returns), including the
OpenAI Agents SDK's own per-run `is_enabled` evaluation.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from hubzoid import capabilities
from hubzoid.access import audit as auditlib
from hubzoid.access import identity_scope
from hubzoid.access.guard import visible
from hubzoid.access.service import Denied
from hubzoid.access.store import GrantStore
from hubzoid.db import operational_url
from hubzoid.server import _enforce_use_hub

from tests.review_fixtures import (
    OPERATIONS,
    OWNER,
    RESTRICTED,
    REVIEW,
    UNRESTRICTED,
    actor,
    call,
    chat_request,
    make_review_deployment,
    payload,
    person,
    registry,
    shown_to,
)

NEW = "new.person@example.org"


@pytest.fixture
def dep(tmp_path, monkeypatch):
    return make_review_deployment(tmp_path, monkeypatch)


def _restricted_tools(hub: str) -> set[str]:
    return {tool for tool, _data in RESTRICTED[hub].values()}


def _new_account(dep, grants=((REVIEW, "use_hub"),)) -> str:
    """An organization administrator creates a login with entry to review-hub."""
    created = dep.svc.create_account(actor(OWNER), email=NEW, name="New Person",
                                     password="a-long-password", grants=list(grants))
    return created["owui_id"]


def _denied(out: str) -> bool:
    return out.startswith("[access denied") and '"demo": true' not in out


# ---- the fixtures match the review hubs ---------------------------------------

def test_fixture_hubs_expose_their_labelled_restricted_catalog(dep):
    for hub in (REVIEW, OPERATIONS):
        entries = {e["permission"]: e for e in dep.svc.catalog(hub) if e["group"] == "restricted"}
        assert set(entries) == set(RESTRICTED[hub])
        assert entries["sample_inventory"]["label"] == "View sample inventory"
        assert all(e["default"] == "grant" for e in entries.values())
        tools = registry(dep.hubs[hub])
        assert set(tools) == _restricted_tools(hub) | {UNRESTRICTED[hub]}
    assert dep.svc.catalog(REVIEW) != dep.svc.catalog(OPERATIONS)


# ---- entry access: unrestricted tools yes, restricted tools no -----------------

def test_a_new_account_with_entry_uses_unrestricted_tools_only(dep):
    account_id = _new_account(dep)
    review = dep.hubs[REVIEW]

    # Chat entry is allowed in review-hub, and only there.
    _enforce_use_hub(chat_request(NEW, account_id), review)
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(chat_request(NEW, account_id), dep.hubs[OPERATIONS])
    assert err.value.status_code == 403

    # The unrestricted hub tool is shown and runs.
    assert shown_to(review, person(NEW)) == {"word_count"}
    assert call(review, "word_count", person(NEW), {"text": "two words"}) == \
        "2 words, 9 chars, 1 lines"

    # Every restricted tool is hidden and a direct call is refused and logged.
    for tool in _restricted_tools(REVIEW):
        assert _denied(call(review, tool, person(NEW)))
    assert {r["decision"] for r in auditlib.read(review)} == {"deny"}
    assert {r["reason"] for r in auditlib.read(review)} == {"no-grant"}


def test_restricted_tools_open_one_permission_at_a_time(dep):
    _new_account(dep)
    review = dep.hubs[REVIEW]
    dep.svc.apply_access_change(actor(OWNER), NEW, REVIEW, [("grant", "sample_budget")])

    assert shown_to(review, person(NEW)) == {"word_count", "read_sample_budget"}
    assert call(review, "read_sample_budget", person(NEW)) == payload(REVIEW, "sample_budget")
    assert _denied(call(review, "read_sample_inventory", person(NEW)))
    assert _denied(call(review, "read_sample_report", person(NEW)))


def test_the_openai_agents_runtime_hides_restricted_tools_itself(dep, monkeypatch):
    """The OpenAI Agents SDK evaluates `is_enabled` per run: the tool list the
    model receives is built by the SDK, not by our filter."""
    from agents import RunContextWrapper

    from hubzoid.factory import build_agent

    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-used")
    _new_account(dep)
    agent = build_agent(dep.hubs[REVIEW])

    def names(ident):
        with identity_scope(ident):
            return {t.name for t in asyncio.run(agent.get_all_tools(RunContextWrapper(context=None)))}

    before = names(person(NEW))
    assert "word_count" in before and not before & _restricted_tools(REVIEW)
    dep.gs.grant(NEW, REVIEW, "sample_inventory", actor="test")
    after = names(person(NEW))
    assert after - before == {"read_sample_inventory"}


# ---- the same permission name in two hubs --------------------------------------

def test_a_grant_in_review_hub_does_not_open_operations_hub(dep):
    _new_account(dep)
    review, operations = dep.hubs[REVIEW], dep.hubs[OPERATIONS]
    dep.svc.apply_access_change(actor(OWNER), NEW, REVIEW, [("grant", "sample_inventory")])

    assert call(review, "read_sample_inventory", person(NEW)) == payload(REVIEW, "sample_inventory")
    assert "read_sample_inventory" in shown_to(review, person(NEW))

    # operations-hub has a tool with the same name under the same permission name.
    assert "read_sample_inventory" in registry(operations)
    assert "read_sample_inventory" not in shown_to(operations, person(NEW))
    out = call(operations, "read_sample_inventory", person(NEW))
    assert _denied(out) and "bolts" not in out
    with pytest.raises(HTTPException):
        _enforce_use_hub(chat_request(NEW), operations)

    assert dep.gs.permissions_for(NEW, REVIEW) == {"use_hub", "sample_inventory"}
    assert dep.gs.permissions_for(NEW, OPERATIONS) == set()
    assert [r["decision"] for r in auditlib.read(operations)] == ["deny"]


# ---- revocation ------------------------------------------------------------------

def test_revocation_applies_on_the_next_call(dep):
    _new_account(dep)
    review = dep.hubs[REVIEW]
    dep.svc.apply_access_change(actor(OWNER), NEW, REVIEW, [("grant", "sample_reports")])
    assert call(review, "read_sample_report", person(NEW)) == payload(REVIEW, "sample_reports")

    # The Console revokes from another process: its own engine on the same store.
    console = GrantStore(create_engine(operational_url(review)))
    console.revoke(NEW, REVIEW, "sample_reports", actor=OWNER)

    assert _denied(call(review, "read_sample_report", person(NEW)))
    assert "read_sample_report" not in shown_to(review, person(NEW))
    # Entry survives a tool revoke; the unrestricted tool still works.
    _enforce_use_hub(chat_request(NEW), review)
    assert call(review, "word_count", person(NEW), {"text": "x"}).startswith("1 words")
    assert [r["decision"] for r in auditlib.read(review)] == ["allow", "deny"]


def test_revoking_entry_through_the_service_removes_every_tool(dep):
    _new_account(dep)
    review = dep.hubs[REVIEW]
    dep.svc.apply_access_change(actor(OWNER), NEW, REVIEW,
                                [("grant", "sample_inventory"), ("grant", "sample_budget")])
    assert call(review, "read_sample_budget", person(NEW)) == payload(REVIEW, "sample_budget")

    dep.svc.apply_access_change(actor(OWNER), NEW, REVIEW, [("revoke", "use_hub")])
    assert _denied(call(review, "read_sample_budget", person(NEW)))
    assert _denied(call(review, "read_sample_inventory", person(NEW)))
    with pytest.raises(HTTPException):
        _enforce_use_hub(chat_request(NEW), review)


# ---- organization administration is not tool access -----------------------------

def test_organization_administration_alone_does_not_run_restricted_tools(dep):
    review, operations = dep.hubs[REVIEW], dep.hubs[OPERATIONS]
    scope = dep.svc.scope(actor(OWNER))
    assert scope.org_admin and set(scope.hubs) == {REVIEW, OPERATIONS}
    # The administrator may grant every restricted capability...
    assert set(RESTRICTED[REVIEW]) <= dep.svc.ceiling(actor(OWNER), REVIEW)

    # ...and may chat, but holds none of them, so none is shown or runs.
    for hub in (REVIEW, OPERATIONS):
        _enforce_use_hub(chat_request(OWNER), dep.hubs[hub])
        assert shown_to(dep.hubs[hub], person(OWNER)) == {UNRESTRICTED[hub]}
        for tool in _restricted_tools(hub):
            assert _denied(call(dep.hubs[hub], tool, person(OWNER)))
        with identity_scope(person(OWNER)):
            assert not any(visible(t) for n, t in registry(dep.hubs[hub]).items()
                           if n != UNRESTRICTED[hub])

    # An explicit grant, even to themselves, is what opens a tool, in one hub.
    dep.svc.apply_access_change(actor(OWNER), OWNER, REVIEW, [("grant", "sample_inventory")])
    assert call(review, "read_sample_inventory", person(OWNER)) == payload(REVIEW, "sample_inventory")
    assert _denied(call(operations, "read_sample_inventory", person(OWNER)))


def test_a_delegate_for_one_hub_cannot_grant_in_the_other(dep):
    """Managing review-hub gives no authority over operations-hub, and a
    delegate grants only what they hold."""
    delegate = "lead@example.org"
    dep.gs.grant(delegate, REVIEW, "manage_access", actor="test")
    dep.gs.grant(delegate, REVIEW, "sample_inventory", actor="test")
    _new_account(dep)

    dep.svc.apply_access_change(actor(delegate), NEW, REVIEW, [("grant", "sample_inventory")])
    assert call(dep.hubs[REVIEW], "read_sample_inventory", person(NEW)) == \
        payload(REVIEW, "sample_inventory")
    for hub, perm, code in ((OPERATIONS, "sample_inventory", "forbidden"),
                            (REVIEW, "sample_budget", "outside_ceiling")):
        with pytest.raises(Denied) as err:
            dep.svc.apply_access_change(actor(delegate), NEW, hub, [("grant", perm)])
        assert err.value.code == code
    assert _denied(call(dep.hubs[OPERATIONS], "read_sample_inventory", person(NEW)))
    # A delegate's own management right runs no tool it was not granted.
    assert _denied(call(dep.hubs[REVIEW], "read_sample_budget", person(delegate)))


def test_the_catalog_is_registered_capabilities_plus_the_hub_files(dep):
    """Built-in capabilities are shared; restricted ones come from each hub."""
    builtin = {c.permission for c in capabilities.registered()}
    review = {e["permission"] for e in dep.svc.catalog(REVIEW)} - builtin
    operations = {e["permission"] for e in dep.svc.catalog(OPERATIONS)} - builtin
    assert review == set(RESTRICTED[REVIEW])
    assert operations == set(RESTRICTED[OPERATIONS])
