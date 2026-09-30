"""The account store (hz_users and friends) on SQLite and PostgreSQL, the
local owner, and the first administrator from the environment."""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from hubzoid.auth import LOCAL_OWNER_EMAIL, passwords, sessions, users
from hubzoid.auth.users import AccountExists, InvalidAccount, UserStore

AUTH_ENV = (
    "HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
    "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD",
    "HUBZOID_ADMIN_NAME", "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD",
    "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_DEPLOYMENT", "DATABASE_URL",
    "HUBZOID_SESSION_DAYS", "HUBZOID_SESSION_IDLE_DAYS",
)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in AUTH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    sessions.reset_cache()
    yield d
    sessions.reset_cache()


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}")
    else:
        base = request.getfixturevalue("postgres_url")
        name = "hz_auth_" + uuid.uuid4().hex[:10]
        admin = create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        admin.dispose()
        engine = create_engine(base.rsplit("/", 1)[0] + "/" + name)
    yield UserStore(engine)
    engine.dispose()


def test_create_normalizes_and_hides_the_hash(store):
    user = store.create(email="  Ana@Example.COM ", name=" Ana ", password="correct horse")
    assert user["email"] == "ana@example.com" and user["name"] == "Ana"
    assert (user["role"], user["status"], user["source"]) == ("user", "active", "admin")
    assert user["password_enabled"] and user["has_password"]
    assert "password_hash" not in user
    assert store.find_by_email("ANA@example.com")["id"] == user["id"]
    stored = store.password_hash(user["id"])
    assert stored.startswith("$argon2id$") and passwords.verify("correct horse", stored)


def test_email_is_unique_and_values_are_checked(store):
    store.create(email="bo@example.com")
    with pytest.raises(AccountExists):
        store.create(email="BO@example.com")
    for bad in (dict(email="not-an-email"), dict(email="x@y.z", role="owner"),
                dict(email="x@y.z", status="blocked"), dict(email="x@y.z", source="elsewhere"),
                dict(email="x@y.z", name="n" * 201)):
        with pytest.raises(InvalidAccount):
            store.create(**bad)


def test_ids_are_kept_when_given_and_new_ones_are_uuids(store):
    kept = store.create(email="migrated@example.com", id="owui-1234", source="migrated")
    assert kept["id"] == "owui-1234"
    fresh = store.create(email="new@example.com")
    assert uuid.UUID(fresh["id"])
    with pytest.raises(AccountExists):
        store.create(email="other@example.com", id="owui-1234")


def test_google_only_accounts_have_no_password(store):
    user = store.create(email="g@example.com", password_enabled=False)
    assert not user["password_enabled"] and users.sign_in_of(user) == "google"
    assert store.password_hash(user["id"]) is None
    with pytest.raises(ValueError):
        store.create(email="h@example.com", password="some password", password_enabled=False)


def _session(store, user_id, token_hash, now=None):
    now = time.time() if now is None else now
    with store.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO hz_sessions (token_hash, user_id, created_at, last_seen_at, expires_at, "
            "idle_seconds) VALUES (:h, :u, :n, :n, :e, 3600)"),
            {"h": token_hash, "u": user_id, "n": now, "e": now + 3600})


def _live(store, user_id):
    with store.engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(text(
            "SELECT token_hash FROM hz_sessions WHERE user_id=:u AND revoked_at IS NULL"),
            {"u": user_id}))


def test_password_role_and_status_changes_end_sessions(store):
    user = store.create(email="c@example.com", password="first password")
    uid = user["id"]
    _session(store, uid, "a" * 64)
    _session(store, uid, "b" * 64)
    store.set_password(uid, "second password", except_token_hash="a" * 64)
    assert _live(store, uid) == ["a" * 64]
    assert passwords.verify("second password", store.password_hash(uid))
    assert store.set_role(uid, "user") is False  # no change: sessions kept
    assert _live(store, uid) == ["a" * 64]
    assert store.set_role(uid, "admin") is True
    assert _live(store, uid) == []
    _session(store, uid, "c" * 64)
    assert store.set_status(uid, "pending") is True
    assert _live(store, uid) == []
    assert store.set_status(uid, "active") is True
    assert store.get(uid)["role"] == "admin"


def test_clearing_a_password_and_renaming(store):
    user = store.create(email="d@example.com", password="some password")
    store.set_password(user["id"], None)
    assert store.password_hash(user["id"]) is None
    assert store.set_name(user["id"], "  Dee ")["name"] == "Dee"
    with pytest.raises(InvalidAccount):
        store.set_name(user["id"], "   ")
    with pytest.raises(KeyError):
        store.set_name("missing", "Name")


def test_delete_removes_sessions_identities_and_links(store):
    user = store.create(email="e@example.com")
    uid = user["id"]
    _session(store, uid, "d" * 64)
    store.link_identity(provider="oidc", issuer="https://idp", subject="s1", user_id=uid,
                        email="e@example.com")
    with store.engine.begin() as conn:
        conn.execute(text("INSERT INTO hz_auth_links (token_hash, user_id, purpose, created_at, "
                          "expires_at) VALUES (:h, :u, 'set_password', 1, 2)"), {"h": "e" * 64, "u": uid})
    assert store.delete(uid) is True
    assert store.get(uid) is None and store.find_identity("https://idp", "s1") is None
    with store.engine.connect() as conn:
        for table in ("hz_sessions", "hz_auth_links"):
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE user_id=:u"),
                                {"u": uid}).scalar() == 0
    assert store.delete(uid) is False


def test_identities_are_keyed_on_issuer_and_subject(store):
    a = store.create(email="f@example.com")
    b = store.create(email="g2@example.com")
    store.link_identity(provider="google", issuer="https://accounts.google.com", subject="123",
                        user_id=a["id"], email="f@example.com")
    with pytest.raises(AccountExists):
        store.link_identity(provider="google", issuer="https://accounts.google.com", subject="123",
                            user_id=b["id"], email="g2@example.com")
    # The same subject at another issuer is another person.
    store.link_identity(provider="oidc", issuer="https://idp.example.com", subject="123",
                        user_id=b["id"], email="g2@example.com")
    assert store.find_identity("https://accounts.google.com", "123")["user_id"] == a["id"]
    assert store.find_identity("https://idp.example.com", "123")["user_id"] == b["id"]
    assert [i["provider"] for i in store.identities_for(b["id"])] == ["oidc"]


def test_admins_touch_login_and_count(store):
    store.create(email="local@localhost", role="admin", source="local")
    admin = store.create(email="admin@example.com", role="admin")
    store.create(email="waiting@example.com", role="admin", status="pending")
    store.create(email="user@example.com")
    assert [u["email"] for u in store.admins()] == ["admin@example.com", "local@localhost"]
    assert [u["email"] for u in store.admins(exclude_local=True)] == ["admin@example.com"]
    assert store.count() == 4 and store.count(exclude_local=True) == 3
    store.touch_login(admin["id"], now=123.0)
    assert store.get(admin["id"])["last_login_at"] == 123.0
    assert [u["email"] for u in store.list()][0] == "admin@example.com"


# ---- the local owner and the first administrator ----------------------------------

def test_local_owner_reuses_the_recorded_account_id(hub):
    from hubzoid.access import store_for

    store_for(hub).upsert_identity(email=LOCAL_OWNER_EMAIL, owui_id="owui-local-7")
    owner = users.ensure_local_owner(hub)
    assert owner["id"] == "owui-local-7"
    assert (owner["role"], owner["status"], owner["source"]) == ("admin", "active", "local")
    assert users.ensure_local_owner(hub)["id"] == "owui-local-7"  # idempotent
    assert sessions.local_owner(hub).id == "owui-local-7"
    assert sessions.local_owner(hub).role == "admin"


def test_local_owner_gets_a_new_id_and_an_identity(hub):
    from hubzoid.access import store_for

    owner = users.ensure_local_owner(hub)
    assert uuid.UUID(owner["id"])
    assert store_for(hub).identity(LOCAL_OWNER_EMAIL)["owui_id"] == owner["id"]


def test_bootstrap_admin_from_env_creates_the_owner_once(hub, monkeypatch):
    from hubzoid.access import store_for

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "Owner@Example.com")
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", "a long enough password")
    user = users.bootstrap_admin_from_env(hub)
    assert user["email"] == "owner@example.com" and user["role"] == "admin"
    assert user["source"] == "bootstrap"
    assert passwords.verify("a long enough password",
                            users.store(hub).password_hash(user["id"]))
    gs = store_for(hub)
    assert gs.can("owner@example.com", "*", "manage_access")
    assert gs.can("owner@example.com", "sales", "use_hub")
    assert gs.identity("owner@example.com")["owui_id"] == user["id"]
    # Accounts exist now: never again, even with other values.
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "other@example.com")
    assert users.bootstrap_admin_from_env(hub) is None
    assert users.find_by_email(hub, "other@example.com") is None


def test_bootstrap_falls_back_to_open_webui_names_and_ignores_the_local_owner(hub, monkeypatch):
    monkeypatch.setenv("WEBUI_AUTH", "true")
    users.ensure_local_owner(hub)  # from an earlier run with sign-in off
    monkeypatch.setenv("WEBUI_ADMIN_EMAIL", "first@example.com")
    monkeypatch.setenv("WEBUI_ADMIN_PASSWORD", "another good password")
    assert users.bootstrap_admin_from_env(hub)["email"] == "first@example.com"


@pytest.mark.parametrize("env", [
    {"HUBZOID_ADMIN_EMAIL": "a@example.com", "HUBZOID_ADMIN_PASSWORD": "short"},
    {"HUBZOID_ADMIN_EMAIL": "not-an-email", "HUBZOID_ADMIN_PASSWORD": "long enough pass"},
    {"HUBZOID_ADMIN_EMAIL": "a@example.com"},
])
def test_bootstrap_refuses_bad_or_partial_settings(hub, monkeypatch, env):
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert users.bootstrap_admin_from_env(hub) is None
    assert users.list_users(hub) == []


def test_bootstrap_does_nothing_with_sign_in_off(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "a@example.com")
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", "long enough pass")
    assert users.bootstrap_admin_from_env(hub) is None


def test_bootstrap_reuses_the_identity_id_so_owner_grants_stay_bound(hub, monkeypatch):
    from hubzoid.access import store_for

    gs = store_for(hub)
    gs.upsert_identity(email="owner@example.com", owui_id="owui-owner")
    gs.grant("owner@example.com", "sales", "use_hub", actor="test")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("HUBZOID_ADMIN_PASSWORD", "long enough pass")
    assert users.bootstrap_admin_from_env(hub)["id"] == "owui-owner"
    assert not gs.is_suspended("owner@example.com")
    assert gs.can("owner@example.com", "sales", "use_hub")


def test_mcp_account_requires_an_active_bound_unblocked_account(hub):
    from hubzoid.access import store_for

    user = users.create(hub, email="m@example.com")
    users.sync_identity(hub, user)
    assert users.mcp_account(hub, email="m@example.com") == {"account_id": user["id"],
                                                               "email": "m@example.com"}
    assert users.mcp_account(hub, account_id=user["id"])["email"] == "m@example.com"
    store_for(hub).suspend("m@example.com", actor="test")
    assert users.mcp_account(hub, email="m@example.com") is None
    pending = users.create(hub, email="p@example.com", status="pending")
    assert users.mcp_account(hub, account_id=pending["id"]) is None
    assert users.mcp_account(hub, email="nobody@example.com") is None
