"""Console account management against a fake Open WebUI (httpx.MockTransport).

The adapter always creates `role: "user"`, never keeps the new account's token,
maps EMAIL_TAKEN, and refuses a public-only URL. The service creates, binds
and grants in one step, undoes a half-created account, and never lets a
password reach the audit log, the access store, a response or a log line.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy import text

from hubzoid import deployment
from hubzoid.access import accounts as accountlib
from hubzoid.access.accounts import AccountError, OwuiAccounts
from hubzoid.access.service import Denied

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    SERVICE,
    FakeOwui,
    actor,
    dep,
    make_deployment,
)

PASSWORD = "Correct-Horse-7-battery"


def _adapter(fake):
    return OwuiAccounts("http://owui.internal", SERVICE, "svc-secret",
                        transport=httpx.MockTransport(fake))


def _everything_stored(dep) -> str:
    """Every row Hubzoid wrote, flattened, for leak checks."""
    out = []
    with dep.gs.engine.connect() as c:
        for table in ("hz_access_audit", "hz_identities", "hz_meta", "hz_change_requests",
                      "hz_grants"):
            out += [repr(tuple(r)) for r in c.execute(text(f"SELECT * FROM {table}"))]
    return "\n".join(out)


# ---- adapter ----------------------------------------------------------------------

def test_create_always_sends_user_role_and_drops_token():
    fake = FakeOwui()
    created = _adapter(fake).create(email="ann@x.org", name="Ann", password=PASSWORD)
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "/api/v1/auths/add")
    assert body["role"] == "user" and body["password"] == PASSWORD
    assert created == {"id": created["id"], "email": "ann@x.org", "name": "Ann", "role": "user"}
    assert "token" not in created
    with pytest.raises(ValueError):
        _adapter(fake).create(email="b@x.org", name="B", password=PASSWORD, role="admin")


def test_email_taken_and_rejections_are_mapped_without_the_password():
    fake = FakeOwui()
    fake.add_user("ann@x.org", "Ann")
    with pytest.raises(AccountError) as e:
        _adapter(fake).create(email="ann@x.org", name="Ann", password=PASSWORD)
    assert (e.value.status, e.value.code) == (409, "account_exists")
    fake.reject_password = True
    with pytest.raises(AccountError) as e:
        _adapter(fake).create(email="new@x.org", name="New", password=PASSWORD)
    assert e.value.code == "rejected" and PASSWORD not in e.value.message


def test_unreachable_chat_app_is_reported_as_uncertain():
    fake = FakeOwui()
    adapter = _adapter(fake)
    adapter.create(email="a@x.org", name="A", password=PASSWORD)  # signs in fine
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if request.url.path == "/api/v1/auths/add":
            raise httpx.ReadTimeout("slow", request=request)
        return fake(request)

    with pytest.raises(AccountError) as e:
        _adapter(flaky).create(email="b@x.org", name="B", password=PASSWORD)
    assert e.value.code == "uncertain" and e.value.certain is False
    fake.down = True
    with pytest.raises(AccountError) as e:
        _adapter(fake).get("x")
    assert e.value.code == "accounts_unavailable"


def test_service_account_must_sign_in():
    with pytest.raises(AccountError) as e:
        OwuiAccounts("http://owui.internal", SERVICE, "wrong",
                     transport=httpx.MockTransport(FakeOwui())).get("x")
    assert e.value.code == "accounts_unavailable"
    assert "wrong" not in e.value.message


def test_refuses_public_only_url(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "OWUI_INTERNAL_URL", "WEBUI_URL", "HUBZOID_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", SERVICE)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "svc-secret")
    monkeypatch.setenv("WEBUI_URL", "https://hub.example.com")
    assert accountlib.internal_url(tmp_path) == ""
    assert not accountlib.configured(tmp_path)
    with pytest.raises(AccountError) as e:
        accountlib.for_deployment(tmp_path)
    assert e.value.code == "accounts_unavailable"
    # An "internal" URL that is really the public one is refused too.
    monkeypatch.setenv("OWUI_INTERNAL_URL", "https://hub.example.com")
    assert accountlib.internal_url(tmp_path) == ""
    monkeypatch.setenv("OWUI_INTERNAL_URL", "http://127.0.0.1:43080")
    assert accountlib.internal_url(tmp_path) == "http://127.0.0.1:43080"
    assert accountlib.configured(tmp_path)


def test_manifest_url_is_internal(dep):
    assert accountlib.internal_url(dep.hub_dir) == "http://owui.internal"
    assert accountlib.configured(dep.hub_dir)


# ---- create --------------------------------------------------------------------------

def test_create_binds_grants_and_audits(dep, caplog):
    caplog.set_level(logging.DEBUG)
    result = dep.svc.create_account(
        actor(DELEGATE), email="Ann@X.org", name="Ann", password=PASSWORD,
        grants=[("finance", "ledger")])
    assert result["subject"] == "ann@x.org" and result["grants"] == {"finance": ["ledger"]}
    assert PASSWORD not in repr(result)
    user = dep.owui.by_email("ann@x.org")
    assert user and user["role"] == "user"
    ident = dep.gs.identity("ann@x.org")
    assert ident["owui_id"] == user["id"] and not ident["pending"]
    assert dep.gs.can("ann@x.org", "finance", "ledger")
    actions = [r["action"] for r in dep.gs.read_access_audit(20, subject="ann@x.org")]
    assert "account_create" in actions and actions.count("grant") == 2
    assert PASSWORD not in _everything_stored(dep)
    assert PASSWORD not in caplog.text


def test_delegate_cannot_create_beyond_ceiling_or_without_access(dep):
    for grants in ([("finance", "payroll")], [("ops", "use_hub")], [("finance", "manage_access")], []):
        with pytest.raises(Denied):
            dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                                   password=PASSWORD, grants=grants)
    # Refused before the chat app was asked to create anything.
    assert not any(p == "/api/v1/auths/add" for _, p, _ in dep.owui.requests)
    assert dep.owui.by_email("ann@x.org") is None


def test_org_admin_may_create_without_access(dep):
    dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD, grants=[])
    assert dep.owui.by_email("ann@x.org")
    assert not dep.gs.permissions_for("ann@x.org", "finance")


def test_existing_account_offers_grant_instead(dep):
    # Known locally: refused before calling the chat app.
    dep.gs.upsert_identity(email="ann@x.org", owui_id="u-ann", display="Ann")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "use_hub")])
    assert (e.value.status, e.value.code) == (409, "account_exists")
    # Unknown locally but taken in the chat app: no grant is applied.
    dep.owui.add_user("bob@x.org", "Bob")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="bob@x.org", name="Bob", password=PASSWORD,
                               grants=[("finance", "ledger")])
    assert e.value.code == "account_exists"
    assert not dep.gs.can("bob@x.org", "finance", "ledger")


def test_failed_grant_deletes_the_new_account(dep, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(dep.gs, "bind_new_account", boom)
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "ledger")])
    assert "removed, so nothing changed" in e.value.message
    assert dep.owui.by_email("ann@x.org") is None
    assert any(m == "DELETE" for m, _, _ in dep.owui.requests)


def test_failed_grant_and_failed_cleanup_is_reported_honestly(dep, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database went away")

    monkeypatch.setattr(dep.gs, "bind_new_account", boom)
    dep.owui.fail["delete"] = 500
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[("finance", "ledger")])
    assert e.value.code == "partial"
    assert "ann@x.org was created" in e.value.message and PASSWORD not in e.value.message
    assert dep.owui.by_email("ann@x.org")  # still there, and the message says so


def test_ceiling_shrinking_mid_create_undoes_the_account(dep, monkeypatch):
    original = dep.directory.create

    def create_then_shrink(**kwargs):
        out = original(**kwargs)
        dep.gs.revoke(DELEGATE, "finance", "ledger", actor="test")
        return out

    monkeypatch.setattr(dep.directory, "create", create_then_shrink)
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(DELEGATE), email="ann@x.org", name="Ann",
                               password=PASSWORD, grants=[("finance", "ledger")])
    assert e.value.code == "outside_ceiling"
    assert dep.owui.by_email("ann@x.org") is None
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_recreating_a_deleted_accounts_email(dep):
    dep.gs.upsert_identity(email="old@x.org", owui_id="u-gone")
    dep.gs.reconcile_accounts([])  # its chat account vanished
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(DELEGATE), email="old@x.org", name="Old",
                               password=PASSWORD, grants=[("finance", "use_hub")])
    assert e.value.code == "account_replaced"
    dep.svc.create_account(actor(ROOT), email="old@x.org", name="Old", password=PASSWORD,
                           grants=[("finance", "use_hub")])
    ident = dep.gs.identity("old@x.org")
    assert ident["owui_id"] != "u-gone" and dep.gs.can("old@x.org", "finance", "use_hub")
    actions = [r["action"] for r in dep.gs.read_access_audit(20, subject="old@x.org")]
    assert "account_replaced" in actions and "account_create" in actions


def test_blocked_email_is_not_recreated(dep):
    dep.gs.suspend("ann@x.org", actor="test")
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=PASSWORD,
                               grants=[])
    assert e.value.code == "blocked"


@pytest.mark.parametrize("password", ["", "short", "x" * 73])
def test_password_rules(dep, password):
    with pytest.raises(Denied) as e:
        dep.svc.create_account(actor(ROOT), email="ann@x.org", name="Ann", password=password,
                               grants=[])
    assert e.value.code == "invalid_password" and (not password or password not in e.value.message)


# ---- account-wide actions (organization administrators) --------------------------------

def _bound(dep, email="ann@x.org", role="user"):
    uid = dep.owui.add_user(email, email.split("@")[0], role=role)
    dep.gs.upsert_identity(email=email, owui_id=uid, pending=role == "pending")
    return uid


def test_password_reset(dep, caplog):
    caplog.set_level(logging.DEBUG)
    uid = _bound(dep)
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(DELEGATE), "ann@x.org", PASSWORD)
    assert e.value.status == 403
    dep.svc.set_password(actor(ROOT), "ann@x.org", PASSWORD)
    assert dep.owui.users[uid].get("password_changed")
    assert "account_password_reset" in [r["action"] for r in dep.gs.read_access_audit(5)]
    assert PASSWORD not in _everything_stored(dep) and PASSWORD not in caplog.text


def test_account_actions_refuse_self_service_account_and_stale_links(dep):
    _bound(dep, ROOT)
    _bound(dep, SERVICE.replace("svc", "svc2"))
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), ROOT, PASSWORD)
    assert e.value.code == "self_change"
    dep.gs.upsert_identity(email=SERVICE, owui_id=dep.owui.by_email(SERVICE)["id"])
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), SERVICE)
    assert e.value.code == "service_account"
    dep.gs.upsert_identity(email="ghost@x.org", owui_id="u-missing")
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), "ghost@x.org", PASSWORD)
    assert e.value.code == "account_changed"
    with pytest.raises(Denied) as e:
        dep.svc.set_password(actor(ROOT), "never@x.org", PASSWORD)
    assert e.value.code == "no_account"


def test_approve_and_role(dep):
    uid = _bound(dep, role="pending")
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "admin")  # approve first
    dep.svc.approve_account(actor(ROOT), "ann@x.org")
    assert dep.owui.users[uid]["role"] == "user"
    assert not dep.gs.is_suspended("ann@x.org")
    with pytest.raises(Denied) as e:
        dep.svc.approve_account(actor(ROOT), "ann@x.org")
    assert e.value.code == "not_pending"
    dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "admin")
    assert dep.owui.users[uid]["role"] == "admin"
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(ROOT), "ann@x.org", "pending")
    with pytest.raises(Denied):
        dep.svc.set_chat_role(actor(DELEGATE), "ann@x.org", "user")


def test_delete_revokes_then_deletes(dep):
    uid = _bound(dep)
    dep.gs.grant("ann@x.org", "finance", "ledger", actor="test")
    with pytest.raises(Denied):
        dep.svc.delete_account(actor(DELEGATE), "ann@x.org")
    dep.svc.delete_account(actor(ROOT), "ann@x.org")
    assert uid not in dep.owui.users
    assert all(g[0] != "ann@x.org" for g in dep.gs.list_grants())
    assert dep.gs.is_suspended("ann@x.org")  # unavailable until re-created
    actions = [r["action"] for r in dep.gs.read_access_audit(5, subject="ann@x.org")]
    assert "revoke_all" in actions and "account_delete" in actions


def test_delete_another_org_admin(dep):
    uid = _bound(dep, "co@x.org")
    dep.gs.grant("co@x.org", "*", "manage_access", actor="test")
    dep.svc.delete_account(actor(ROOT), "co@x.org")
    assert uid not in dep.owui.users and not dep.gs.can("co@x.org", "*", "manage_access")


def test_delete_stops_when_last_admin_guard_trips(dep, monkeypatch):
    from hubzoid.access.store import LastAdminError

    uid = _bound(dep, "co@x.org")

    def guard(*_a, **_k):
        raise LastAdminError("cannot remove the last org admin; grant another first")

    monkeypatch.setattr(dep.gs, "revoke_all", guard)
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), "co@x.org")
    assert (e.value.status, e.value.code) == (409, "last_admin")
    assert uid in dep.owui.users  # the chat account is untouched


def test_delete_when_chat_app_fails_reports_partial(dep):
    _bound(dep)
    dep.gs.grant("ann@x.org", "finance", "ledger", actor="test")
    dep.owui.fail["delete"] = 500
    with pytest.raises(Denied) as e:
        dep.svc.delete_account(actor(ROOT), "ann@x.org")
    assert e.value.code == "partial" and "Access was removed" in e.value.message
    assert not dep.gs.can("ann@x.org", "finance", "ledger")


def test_manifest_owui_url_used_by_default(dep):
    assert deployment.read(dep.hub_dir)["owui_url"] == "http://owui.internal"
