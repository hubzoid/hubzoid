"""One Administrator role: Hubzoid administration and the chat app's admin role
are set together, partial results are reported, mismatches are never fixed
implicitly, and the last administrator is protected."""
from __future__ import annotations

import pytest

from hubzoid.access.service import SIGN_IN_PREFIX, Denied
from tests.test_access_service import ROOT, actor, make_deployment
from tests.test_accounts_owui import PASSWORD, _bound


@pytest.fixture
def dep(tmp_path, monkeypatch):
    return make_deployment(tmp_path, monkeypatch)


def _console_admin(dep, email):
    return dep.gs.can(email, "*", "manage_access")


def test_administrator_sets_both_sides_and_user_clears_both(dep):
    _bound(dep, ROOT, role="admin")   # another administrator, so co can be demoted again
    uid = _bound(dep, "co@x.org")
    out = dep.svc.set_role(actor(ROOT), "co@x.org", "admin")
    assert out["administrator"] == "admin"
    assert dep.owui.users[uid]["role"] == "admin" and _console_admin(dep, "co@x.org")
    assert dep.svc.account_info(actor(ROOT), "co@x.org")["administrator"] == "admin"
    dep.svc.set_role(actor(ROOT), "co@x.org", "user")
    assert dep.owui.users[uid]["role"] == "user" and not _console_admin(dep, "co@x.org")


def test_console_side_failure_is_reported_and_a_retry_finishes(dep, monkeypatch):
    uid = _bound(dep, "co@x.org")
    real = dep.svc.apply_access_change

    def refuse(*a, **k):
        raise Denied(503, "store_unavailable", "The access store is unavailable.")

    monkeypatch.setattr(dep.svc, "apply_access_change", refuse)
    with pytest.raises(Denied) as e:
        dep.svc.set_role(actor(ROOT), "co@x.org", "admin")
    assert (e.value.status, e.value.code) == (502, "role_partial")
    assert "administrator in the chat app but not in the Console" in e.value.message
    assert e.value.extra["retry"] is True and e.value.extra["administrator"] == "chat_only"
    assert dep.owui.users[uid]["role"] == "admin" and not _console_admin(dep, "co@x.org")
    monkeypatch.setattr(dep.svc, "apply_access_change", real)
    out = dep.svc.set_role(actor(ROOT), "co@x.org", "admin")   # the retry completes it
    assert out["administrator"] == "admin" and _console_admin(dep, "co@x.org")


@pytest.mark.parametrize("status, wording", [
    (400, "Nothing was changed"),                         # a clear refusal
    (500, "Console administration was left as it was")])  # unconfirmed: never guessed
def test_chat_app_failure_leaves_console_administration_alone(dep, status, wording):
    uid = _bound(dep, "co@x.org")
    dep.owui.fail["update"] = status
    with pytest.raises(Denied) as e:
        dep.svc.set_role(actor(ROOT), "co@x.org", "admin")
    assert wording in e.value.message
    assert dep.owui.users[uid]["role"] == "user" and not _console_admin(dep, "co@x.org")


@pytest.mark.parametrize("chat_role, console, expected", [
    ("admin", False, "chat_only"), ("user", True, "console_only")])
def test_mismatched_roles_are_shown_and_never_promoted(dep, chat_role, console, expected):
    uid = _bound(dep, "co@x.org", role=chat_role)
    if console:
        dep.gs.grant("co@x.org", "*", "manage_access", actor="test")
    revision = dep.gs.revision()
    info = dep.svc.account_info(actor(ROOT), "co@x.org")
    assert info["administrator"] == expected
    # Reading never fixes it: both sides stay exactly as they were.
    assert dep.gs.revision() == revision and dep.owui.users[uid]["role"] == chat_role
    assert _console_admin(dep, "co@x.org") is console


def test_last_chat_app_administrator_cannot_be_demoted_or_deleted(dep):
    # co is the only chat-app administrator besides the Console's service account.
    uid = _bound(dep, "co@x.org", role="admin")
    with pytest.raises(Denied) as e:
        dep.svc.set_role(actor(ROOT), "co@x.org", "user")
    assert e.value.code == "last_admin"
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), "co@x.org")
    assert e.value.code == "last_admin"
    assert dep.owui.users[uid]["role"] == "admin"
    # With another administrator, the same demotion works.
    _bound(dep, "other@x.org", role="admin")
    dep.svc.set_role(actor(ROOT), "co@x.org", "user")
    assert dep.owui.users[uid]["role"] == "user"


def test_nobody_changes_their_own_role(dep):
    _bound(dep, ROOT, role="admin")
    with pytest.raises(Denied):
        dep.svc.set_role(actor(ROOT), ROOT, "user")


def test_google_only_password_is_managed_by_google(dep):
    uid = _bound(dep, "g@x.org")
    dep.gs.set_metadata(SIGN_IN_PREFIX + "g@x.org", "google")
    assert dep.svc.account_info(actor(ROOT), "g@x.org")["sign_in"] == "google"
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), "g@x.org", PASSWORD)
    assert e.value.code == "google_managed"
    assert "password_changed" not in dep.owui.users[uid]
