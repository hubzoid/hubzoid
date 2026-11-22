"""Directory provider, token, account and replay state without an identity provider."""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from hubzoid.auth import users
from hubzoid.auth.directory import store
from tests.test_auth_mfa import hub  # noqa: F401


def provision(hub, email="ana@example.com", **kw):
    return store.provision(hub, "okta", user_name=email, email=email, name="Ana",
                           external_id="immutable-" + email, active=kw.pop("active", True),
                           resource=kw)


def replace(hub, account, **kw):
    return store.update(hub, "okta", account["id"], user_name=kw.pop("user_name", account["user_name"]),
                        name=kw.pop("name", None), external_id=None,
                        active=kw.pop("active", True), resource={}, **kw)


@pytest.mark.parametrize("pid,name,code", [("X", "Okta", "invalid_provider"),
    ("okta", " ", "invalid_name"), ("okta", "x" * 121, "invalid_name")])
def test_provider_validation(hub, pid, name, code):
    with pytest.raises(store.DirectoryError) as exc:
        store.save_provider(hub, provider_id=pid, name=name, config={}, actor="test")
    assert (exc.value.status, exc.value.code) == (422, code)
    assert store.list_providers(hub) == []


def test_provider_lifecycle_and_tokens(hub, monkeypatch):
    p = store.save_provider(hub, provider_id="okta", name=" Okta ", config={"a": 1}, actor="a")
    assert p["name"] == "Okta" and not p["enabled"]
    assert store.list_providers(hub, enabled_only=True) == []
    updated = store.save_provider(hub, provider_id="okta", name="Company", config={}, actor="b", enabled=True)
    assert updated["enabled"] and updated["updated_by"] == "b"
    assert store.list_providers(hub, enabled_only=True)[0]["id"] == "okta"
    meta, token = store.create_token(hub, "okta", name="", actor="b")
    assert meta["name"] == "SCIM" and token.startswith("hzs_")
    assert store.token_provider(hub, token) == "okta"
    used = store.list_tokens(hub, "okta")[0]["last_used_at"]
    assert used is not None
    assert store.token_provider(hub, token) == "okta"
    assert store.list_tokens(hub, "okta")[0]["last_used_at"] == used
    monkeypatch.setattr(store.time, "time", lambda: used + 61)
    assert store.token_provider(hub, token) == "okta"
    assert store.list_tokens(hub, "okta")[0]["last_used_at"] == used + 61
    assert not store.revoke_token(hub, "other", meta["id"])
    assert store.revoke_token(hub, "okta", meta["id"])
    assert not store.revoke_token(hub, "okta", meta["id"])
    assert store.token_provider(hub, token) is None
    assert "token_hash" not in store.list_tokens(hub, "okta")[0]
    assert store.delete_provider(hub, "okta")
    assert store.list_tokens(hub, "okta") == []
    assert not store.delete_provider(hub, "okta")


@pytest.mark.parametrize("token", ["", "bad", "hzs_" + "x" * 201, "hzs_unknown"])
def test_invalid_tokens_have_no_provider(hub, token):
    assert store.token_provider(hub, token) is None


def test_provisioned_names_are_scoped_and_unique(hub):
    account = provision(hub)
    assert account["version"] == 1 and account["resource"]["id"] == account["id"]
    user = users.get(hub, account["user_id"])
    assert user["managed_by"] == "okta" and not user["has_password"]
    assert store.for_name_id(hub, "okta", account["external_id"])[0]["id"] == account["id"]
    assert store.for_name_id(hub, "okta", " ANA@EXAMPLE.COM ")[0]["id"] == account["id"]
    assert store.by_external_id(hub, "okta", "") == []
    assert store.by_user_name(hub, "other", account["user_name"]) == []
    with pytest.raises(store.DirectoryError, match="userName already") as exc:
        provision(hub)
    assert exc.value.status == 409
    with pytest.raises(store.DirectoryError, match="email already") as exc:
        store.provision(hub, "other", user_name="other", email="ana@example.com", name="",
                        external_id=None, active=True, resource={})
    assert exc.value.code == "uniqueness"


def test_deactivation_reactivation_versions_and_delete(hub):
    account = provision(hub, active=False)
    assert users.get(hub, account["user_id"])["status"] == "disabled"
    active = replace(hub, account, name="New name")
    assert active["active"] and active["version"] == 2
    assert users.get(hub, account["user_id"])["name"] == "New name"
    assert users.get(hub, account["user_id"])["status"] == "active"
    with pytest.raises(store.DirectoryError) as stale:
        replace(hub, active, expected_version=1)
    assert stale.value.status == 412
    other = provision(hub, "ben@example.com")
    with pytest.raises(store.DirectoryError) as clash:
        replace(hub, active, user_name=other["user_name"])
    assert clash.value.code == "uniqueness"
    with pytest.raises(store.DirectoryError) as absent:
        replace(hub, {**active, "id": "missing"})
    assert absent.value.status == 404
    store.save_provider(hub, provider_id="okta", name="Okta", config={}, actor="a")
    with pytest.raises(store.DirectoryError) as live:
        store.delete_provider(hub, "okta")
    assert live.value.code == "provider_in_use"
    assert store.deprovision(hub, "okta", account["id"])
    assert users.get(hub, account["user_id"])["status"] == "disabled"
    assert store.get_account(hub, "okta", account["id"]) is None
    assert not store.deprovision(hub, "okta", account["id"])
    assert [a["id"] for a in store.accounts_for(hub, "okta")] == [other["id"]]


def test_missing_local_account_state_update_is_harmless(hub):
    with store._engine(hub).begin() as conn:
        store._set_user_state(conn, "missing", False, 10)
        assert conn.execute(sa.select(sa.func.count()).select_from(store.accounts)).scalar_one() == 0


def test_replay_is_provider_scoped_and_retained_at_least_five_minutes(hub, monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 1000)
    assert store.first_use(hub, "okta", "a", 900)
    assert not store.first_use(hub, "okta", "a", 900)
    assert store.first_use(hub, "other", "a", 900)
    monkeypatch.setattr(store.time, "time", lambda: 1301)
    assert store.first_use(hub, "okta", "a", 2000)
