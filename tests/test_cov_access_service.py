"""Access-service refusals, account cleanup and partial-write reporting."""
from __future__ import annotations

import pytest

from hubzoid.access import service
from hubzoid.access.accounts import AccountError
from hubzoid.access.store import RevisionConflict
from tests.test_access_service import DELEGATE, ROOT, actor, dep  # noqa: F401


@pytest.mark.parametrize("password", [None, "", "short", "界" * 25])
def test_password_limits_never_echo_input(password):
    with pytest.raises(service.Denied) as exc:
        service.check_password(password)
    assert exc.value.status == 422 and exc.value.code == "invalid_password"
    if password:
        assert password not in exc.value.message


@pytest.mark.parametrize("raw,wanted", [("bad", 900), ("1", 60), ("90000", 86400), ("100", 100)])
def test_request_ttl_is_bounded(monkeypatch, raw, wanted):
    monkeypatch.setenv("HUBZOID_CHANGE_REQUEST_TTL", raw)
    assert service.ttl() == wanted


@pytest.mark.parametrize("plan,code", [(None, "invalid_plan"), ({}, "invalid_plan"),
    ({"hub": "*"}, "invalid_plan"), ({"hub": "finance", "kind": "unknown"}, "invalid_plan"),
    ({"hub": "finance", "kind": "access", "subject": "ana@x.org", "grant": "ledger"}, "invalid_plan"),
    ({"hub": "finance", "kind": "access", "subject": "", "grant": ["ledger"]}, "invalid_plan"),
    ({"hub": "finance", "kind": "access", "subject": "ana@x.org"}, "invalid_plan"),
    ({"hub": "finance", "kind": "access", "subject": "ana@x.org", "grant": ["ledger"], "revoke": ["ledger"]}, "invalid_plan"),
    ({"hub": "finance", "kind": "account", "email": "bad", "name": "Ana"}, "invalid_email"),
    ({"hub": "finance", "kind": "account", "email": "ana@x.org", "name": ""}, "invalid_name"),
    ({"hub": "finance", "kind": "account", "email": "ana@x.org", "name": "Ana", "revoke": ["ledger"]}, "invalid_plan")])
def test_invalid_plans_never_create_pending_change(dep, plan, code):
    with pytest.raises(service.Denied) as exc:
        dep.svc.propose(actor(ROOT), plan)
    assert exc.value.code == code and exc.value.status == 422


@pytest.mark.parametrize("grants", [[("", "use_hub")], [("finance", "")], [("*", "manage_access")]])
def test_initial_grants_need_concrete_hub_and_permission(dep, grants):
    with pytest.raises(service.Denied) as exc:
        dep.svc.create_account(actor(ROOT), email="ana@x.org", name="Ana", password="long-password", grants=grants)
    assert exc.value.code == "invalid_grant"
    assert dep.owui.by_email("ana@x.org") is None


@pytest.mark.parametrize("subject,code", [("", "invalid_subject"), ("*", "invalid_subject"),
    ("workflow:daily", "invalid_subject"), (ROOT, "self_change"), ("missing@x.org", "no_account")])
def test_account_actions_refuse_invalid_targets(dep, subject, code):
    with pytest.raises(service.Denied) as exc:
        dep.svc.approve_account(actor(ROOT), subject)
    assert exc.value.code == code


def test_unexpected_created_account_is_removed_or_reported(dep, monkeypatch):
    def create(**kw):
        uid = dep.owui.add_user("wrong@x.org", "Wrong")
        return dep.owui.get(uid)
    monkeypatch.setattr(dep.owui, "create", create)
    with pytest.raises(service.Denied) as exc:
        dep.svc.create_account(actor(ROOT), email="ana@x.org", name="Ana", password="long-password", grants=[])
    assert exc.value.code == "chat_app_error" and "it was removed" in exc.value.message
    assert dep.owui.by_email("wrong@x.org") is None
    dep.owui.fail["delete"] = 500
    with pytest.raises(service.Denied) as exc:
        dep.svc.create_account(actor(ROOT), email="ana@x.org", name="Ana", password="long-password", grants=[])
    assert "could not be removed" in exc.value.message and dep.owui.by_email("wrong@x.org") is not None


def test_account_binding_retries_then_reports_partial(dep, monkeypatch):
    calls = []
    def conflicted(*a, **kw):
        calls.append(kw)
        raise RevisionConflict()
    monkeypatch.setattr(dep.gs, "bind_new_account", conflicted)
    with pytest.raises(service.Denied) as exc:
        dep.svc.create_account(actor(ROOT), email="ana@x.org", name="Ana", password="long-password", grants=[("finance", "ledger")])
    assert exc.value.code == "partial" and exc.value.extra["recorded"] is False
    assert exc.value.extra["access_granted"] is False and len(calls) == 4
    assert dep.owui.by_email("ana@x.org") is not None
    assert not dep.gs.can("ana@x.org", "finance", "ledger")


def test_link_creation_failure_keeps_created_account(dep):
    class Links:
        def issue_link(self, *a, **kw):
            raise RuntimeError("offline")
    result = dep.svc._sign_in_link(Links(), {"id": "new"}, actor(ROOT))
    assert result["link"] is None and "Use Reset password" in result["link_error"]


def test_google_setup_and_invalid_sign_in_refusals(dep, monkeypatch):
    with pytest.raises(service.Denied) as exc:
        dep.svc._new_password("ana@x.org", None, "unknown")
    assert exc.value.code == "invalid_sign_in"
    with pytest.raises(service.Denied) as exc:
        dep.svc._new_password("ana@x.org", "password", "google")
    assert exc.value.code == "invalid_request"
    monkeypatch.setattr(dep.svc, "sign_in_options", lambda: {"google": False})
    with pytest.raises(service.Denied) as exc:
        dep.svc._check_google("ana@x.org")
    assert exc.value.code == "google_unavailable"
    monkeypatch.setattr(dep.svc, "sign_in_options", lambda: {"google": True, "google_domains": ["other.org"]})
    with pytest.raises(service.Denied) as exc:
        dep.svc._check_google("ana@x.org")
    assert exc.value.code == "google_domain"


def test_live_account_mismatch_and_directory_errors(dep):
    uid = dep.owui.add_user("ana@x.org", "Ana")
    with pytest.raises(service.Denied) as exc:
        dep.svc._live_account(dep.owui, {"owui_id": uid, "subject": "other@x.org"})
    assert exc.value.code == "account_changed"
    def failed():
        raise AccountError(503, "unavailable", "offline")
    with pytest.raises(service.Denied) as exc:
        dep.svc._directory_call(failed)
    assert (exc.value.status, exc.value.code) == (503, "unavailable")
    assert dep.svc._other_chat_admins(object(), ROOT) is None
    dep.owui.down = True
    assert dep.svc._other_chat_admins(dep.owui, ROOT) is None


def test_phone_validation_and_read_only_check(dep):
    with pytest.raises(service.Denied) as exc:
        dep.svc.set_phone(actor(ROOT), "workflow:daily", "+15550001111")
    assert exc.value.code == "invalid_subject"
    with pytest.raises(service.Denied) as exc:
        dep.svc.check_phone("ana@x.org", "123")
    assert exc.value.code == "invalid_phone"
    dep.gs.set_phone("ben@x.org", "+15550001111", actor=ROOT)
    with pytest.raises(service.Denied) as exc:
        dep.svc.check_phone("ana@x.org", "+15550001111")
    assert exc.value.code == "phone_taken"
    assert dep.gs.subject_for_phone("15550001111") == "ben@x.org"


def test_delegate_new_account_requires_access(dep):
    with pytest.raises(service.Denied) as exc:
        dep.svc.create_account(actor(DELEGATE), email="ana@x.org", name="Ana", password="long-password", grants=[])
    assert exc.value.code == "grant_required" and dep.owui.by_email("ana@x.org") is None
