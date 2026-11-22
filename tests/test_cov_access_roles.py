"""Roles, deleting people and the checks before a new account
(hubzoid.access.service): only valid roles, never your own, a pending account
is approved first, a retry after success changes nothing; deleting removes
the account or says exactly what is left; a delegate must give access in an
agent they manage, a blocked person gets none, an existing account is not
created twice. Uses the in-memory account directory of
tests/test_access_service.py."""
from __future__ import annotations

import pytest

from hubzoid.access import service
from tests.test_access_service import DELEGATE, ROOT, actor, dep  # noqa: F401


def denied(fn, *a, **kw):
    with pytest.raises(service.Denied) as err:
        fn(*a, **kw)
    return err.value


def new_person(dep, email="ana@x.org"):  # noqa: F811
    dep.svc.create_account(actor(ROOT), email=email, name="Ana", password="long-password", grants=[])
    return email


def test_roles(dep):  # noqa: F811
    ana = new_person(dep)
    assert denied(dep.svc.set_role, actor(ROOT), ana, "wizard").code == "invalid_role"
    assert denied(dep.svc.set_role, actor(ROOT), ROOT, "user").code == "self_change"
    out = dep.svc.set_role(actor(ROOT), ana, "admin")
    assert out["changed"] is True and out["role"] == "admin" and dep.owui.by_email(ana)["role"] == "admin"
    again = dep.svc.set_role(actor(ROOT), ana, "admin")
    assert again["changed"] is False                                   # a retry finishes nothing new
    # Ana is now the chat app's only administrator: she can't be demoted.
    assert denied(dep.svc.set_role, actor(ROOT), ana, "user").code == "last_admin"
    dep.owui.add_user("other-admin@x.org", "Other", role="admin")
    back = dep.svc.set_role(actor(ROOT), ana, "user")
    assert back["changed"] is True and dep.owui.by_email(ana)["role"] == "user"
    dep.owui.by_email(ana)["role"] = "pending"
    assert denied(dep.svc.set_role, actor(ROOT), ana, "admin").code == "pending"


def test_deleting_a_person(dep):  # noqa: F811
    ana = new_person(dep)
    dep.svc.delete_account(actor(ROOT), ana)
    assert dep.owui.by_email(ana) is None
    ben = new_person(dep, "ben@x.org")
    dep.owui.fail["delete"] = 500
    err = denied(dep.svc.delete_account, actor(ROOT), ben)
    assert err.status == 502 and err.code == "partial" and "Access was removed" in err.message
    assert dep.owui.by_email(ben) is not None


def test_checks_before_a_new_account(dep):  # noqa: F811
    err = denied(dep.svc.create_account, actor(DELEGATE), email="cy@x.org", name="Cy",
                 password="long-password", grants=[])
    assert err.code == "grant_required"
    existing = new_person(dep)
    err = denied(dep.svc.create_account, actor(ROOT), email=existing, name="Ana", password="long-password", grants=[])
    assert err.code == "account_exists"
    dep.svc.store.suspend("blocked@x.org", actor=ROOT)
    err = denied(dep.svc.create_account, actor(ROOT), email="blocked@x.org", name="B", password="long-password",
                 grants=[])
    assert err.code == "blocked" and dep.owui.by_email("blocked@x.org") is None
