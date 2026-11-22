"""The access service when something it depends on fails or the request is
wrong (hubzoid.access.service): an unreadable deployment or access store is
503 (never a guess at more access), an unknown agent 404, the catalogue still
lists without the grants; phone numbers: only a person's account, a number
someone else has is refused, a bad number names the problem; approving an
account that isn't pending, the last administrator kept when deleting, a lost
deletion marker tolerated; a change can't be proposed from an unknown
surface. Uses the in-memory deployment of tests/test_access_service.py."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.access import service
from hubzoid.access.service import Denied
from tests.test_access_service import ROOT, actor, dep  # noqa: F401


def denied(fn, *a, **kw):
    with pytest.raises(Denied) as err:
        fn(*a, **kw)
    return err.value


def test_an_unreadable_deployment_or_store_is_503(dep, monkeypatch):  # noqa: F811
    assert denied(dep.svc.catalog, "nowhere").status == 404
    real_hubs = service.deployment.hubs

    def unreadable(hub_dir):
        raise OSError("deployment.json unreadable")

    monkeypatch.setattr(service.deployment, "hubs", unreadable)
    err = denied(dep.svc.catalog, "finance")
    assert (err.status, err.code) == (503, "deployment_unavailable")
    monkeypatch.setattr(service.deployment, "hubs", real_hubs)

    def store_down(*a, **kw):
        raise RuntimeError("database gone")

    monkeypatch.setattr(dep.gs, "can", store_down)
    err = denied(dep.svc.scope, actor(ROOT))
    assert (err.status, err.code) == (503, "store_unavailable")


def test_the_catalogue_lists_without_the_grants(dep, monkeypatch):  # noqa: F811
    def unreadable(*a, **kw):
        raise RuntimeError("grants table locked")

    monkeypatch.setattr(dep.gs, "list_grants", unreadable)
    ids = {c["permission"] for c in dep.svc.catalog("finance")}
    assert {"ledger", "payroll"} <= ids and not any(c.get("obsolete") for c in dep.svc.catalog("finance"))


def test_links_mode_follows_the_directory(dep):  # noqa: F811
    assert dep.svc.links_mode() is False
    linked = service.AccessService(dep.hub_dir, accounts=SimpleNamespace(links=True))
    assert linked.links_mode() is True


def test_phone_numbers(dep):  # noqa: F811
    assert denied(dep.svc.set_phone, actor(ROOT), "*", "+44 7700 900123").code == "invalid_subject"
    assert denied(dep.svc.set_phone, actor(ROOT), "workflow:daily", None).code == "invalid_subject"
    assert denied(dep.svc.set_phone, actor(ROOT), "ana@x.org", "call me").code == "invalid_phone"
    out = dep.svc.set_phone(actor(ROOT), "ana@x.org", "+44 7700 900123")
    assert out["subject"] == "ana@x.org" and out["phone"]
    err = denied(dep.svc.set_phone, actor(ROOT), "ben@x.org", "+44 7700 900123")
    assert (err.status, err.code) == (409, "phone_taken")
    assert dep.svc.set_phone(actor(ROOT), "ana@x.org", None)["phone"] is None


def test_accounts_that_cant_be_approved_or_deleted(dep, monkeypatch):  # noqa: F811
    dep.svc.create_account(actor(ROOT), email="ana@x.org", name="Ana", password="long-password", grants=[])
    assert denied(dep.svc.approve_account, actor(ROOT), "ana@x.org").code == "not_pending"

    def last_admin(*a, **kw):
        raise service.LastAdminError("Ana is the last administrator.")

    monkeypatch.setattr(dep.gs, "revoke_all", last_admin)
    err = denied(dep.svc.delete_account, actor(ROOT), "ana@x.org")
    assert (err.status, err.code) == (409, "last_admin") and dep.owui.by_email("ana@x.org") is not None
    monkeypatch.undo()

    def no_marker(*a, **kw):
        raise RuntimeError("metadata table locked")

    monkeypatch.setattr(dep.gs, "set_metadata", no_marker)
    dep.svc.delete_account(actor(ROOT), "ana@x.org")                 # deleted all the same
    assert dep.owui.by_email("ana@x.org") is None


def test_a_change_cant_be_proposed_from_an_unknown_surface(dep):  # noqa: F811
    err = denied(dep.svc.propose, actor(ROOT, surface="fax"), {"subject": "ana@x.org"})
    assert (err.status, err.code) == (403, "surface")
