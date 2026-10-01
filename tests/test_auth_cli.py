"""``hubzoid admin``: create accounts (link, typed password, Google only,
administrator, owner), reset passwords, list, set roles. Passwords come from a
hidden prompt and never appear in the output."""
from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from hubzoid.access import store_for
from hubzoid.auth import links, passwords, sessions, users
from hubzoid.cli import app

ENV = (
    "HUBZOID_UI", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_ALLOWED_ORIGINS",
    "HUBZOID_ADMIN_EMAIL", "HUBZOID_ADMIN_PASSWORD", "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD",
    "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "PORT",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_MERGE_ACCOUNTS_BY_EMAIL",
)
LINK = re.compile(r"(http://\S+/auth/set-password\?token=[A-Za-z0-9_-]+)")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales\n---\nHelp.\n")
    sessions.reset_cache()
    return d


def run(*args, input=None):  # noqa: A002 — the prompt's input
    result = CliRunner().invoke(app, ["admin", *args], input=input)
    return result.exit_code, result.output


def token_of(output: str) -> str:
    """The token from the printed link (printed on one line, never wrapped)."""
    return LINK.search(output).group(1).rsplit("token=", 1)[1]


def test_create_prints_a_one_time_link(hub):
    code, out = run("create", "Ana@Example.com", str(hub), "--name", "Ana")
    assert code == 0, out
    assert "Created user ana@example.com" in out and "Expires" in out
    assert "http://127.0.0.1:3080/auth/set-password?token=" in out
    user = users.find_by_email(hub, "ana@example.com")
    assert (user["name"], user["role"], user["has_password"]) == ("Ana", "user", False)
    assert links.inspect(hub, token_of(out)) == {"valid": True, "purpose": "set_password",
                                                 "email": "ana@example.com"}
    identity = store_for(hub).identity("ana@example.com")
    assert identity["owui_id"] == user["id"]
    rows = store_for(hub).read_access_audit(5, action="account_create")
    assert rows and rows[0]["actor"].startswith("cli:")


def test_links_use_the_public_url_or_the_port(hub, monkeypatch):
    monkeypatch.setenv("PORT", "3290")
    assert "http://127.0.0.1:3290/auth/set-password" in run("create", "a@example.com", str(hub))[1]
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com/")
    out = run("create", "b@example.com", str(hub))[1]
    assert "https://hub.example.com/auth/set-password?token=" in out


def test_create_an_administrator_and_an_owner(hub):
    code, out = run("create", "boss@example.com", str(hub), "--admin")
    assert code == 0 and "Created administrator" in out
    gs = store_for(hub)
    assert users.find_by_email(hub, "boss@example.com")["role"] == "admin"
    assert gs.can("boss@example.com", "*", "manage_access")
    code, out = run("create", "owner@example.com", str(hub), "--owner")
    assert code == 0 and "Owner access on: sales." in out
    assert gs.can("owner@example.com", "sales", "use_hub")
    assert gs.can("owner@example.com", "*", "manage_access")
    assert users.find_by_email(hub, "owner@example.com")["role"] == "admin"


def test_create_with_a_typed_password(hub):
    code, out = run("create", "typed@example.com", str(hub), "--password",
                    input="typed secret one\ntyped secret one\n")
    assert code == 0, out
    assert "typed secret one" not in out and "set-password" not in out
    user = users.find_by_email(hub, "typed@example.com")
    assert passwords.verify("typed secret one", users.store(hub).password_hash(user["id"]))
    code, out = run("create", "weak@example.com", str(hub), "--password", input="short\nshort\n")
    assert code == 2 and "at least 8 characters" in out
    assert users.find_by_email(hub, "weak@example.com") is None


def test_create_google_only(hub, monkeypatch):
    code, out = run("create", "g@example.com", str(hub), "--google-only")
    assert code == 0 and "can't sign in until it is" in out and "set-password" not in out
    assert not users.find_by_email(hub, "g@example.com")["password_enabled"]
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    code, out = run("create", "g2@example.com", str(hub), "--google-only")
    assert code == 0 and "can't sign in" not in out and "They sign in with Google" in out
    assert run("create", "g3@example.com", str(hub), "--google-only", "--password")[0] == 2


def test_create_refusals(hub, tmp_path):
    run("create", "dup@example.com", str(hub))
    code, out = run("create", "DUP@example.com", str(hub))
    assert code == 1 and "already exists" in out
    assert run("create", "not-an-email", str(hub))[0] == 2
    store_for(hub).suspend("blocked@example.com", actor="test")
    code, out = run("create", "blocked@example.com", str(hub))
    assert code == 1 and "blocked" in out
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = run("create", "x@example.com", str(empty))
    assert code == 2 and "not a hub" in out


def test_create_replaces_an_earlier_accounts_access(hub):
    gs = store_for(hub)
    gs.upsert_identity(email="reused@example.com", owui_id="old-account")
    gs.grant("reused@example.com", "sales", "use_hub", actor="test")
    code, out = run("create", "reused@example.com", str(hub))
    assert code == 0 and "earlier account" in out
    assert not gs.can("reused@example.com", "sales", "use_hub")
    assert not gs.is_suspended("reused@example.com")


def test_reset_password_with_a_link_or_a_prompt(hub):
    user = users.create(hub, email="r@example.com", password="old password 1")
    token = sessions.create_session(hub, user, method="password")
    code, out = run("reset-password", "r@example.com", str(hub))
    assert code == 0 and "signed out" in out
    assert users.store(hub).password_hash(user["id"]) is None
    assert sessions.resolve_token(hub, token) is None
    first = token_of(out)
    assert links.inspect(hub, first)["purpose"] == "reset_password"
    code, out = run("reset-password", "r@example.com", str(hub), "--password",
                    input="new password 2\nnew password 2\n")
    assert code == 0 and "new password 2" not in out
    assert passwords.verify("new password 2", users.store(hub).password_hash(user["id"]))
    assert not links.inspect(hub, first)["valid"]  # the typed password cancels the link
    users.create(hub, email="g@example.com", password_enabled=False)
    assert run("reset-password", "g@example.com", str(hub))[0] == 1
    assert run("reset-password", "nobody@example.com", str(hub))[0] == 1


def test_list(hub):
    users.create(hub, email="a@example.com", name="Ann", role="admin", password="password 123")
    users.create(hub, email="b@example.com", name="Bo", password_enabled=False, status="pending")
    code, out = run("list", str(hub))
    assert code == 0
    assert "a@example.com" in out and "Administrator" in out and "b@example.com" in out
    assert "pending" in out and "google" in out and "2 accounts." in out


def test_set_role(hub):
    run("create", "first@example.com", str(hub), "--admin")
    run("create", "second@example.com", str(hub))
    second = users.find_by_email(hub, "second@example.com")
    token = sessions.create_session(hub, second, method="password")
    code, out = run("set-role", "second@example.com", "admin", str(hub))
    assert code == 0 and "is now an Administrator" in out and "signed out" in out
    gs = store_for(hub)
    assert gs.can("second@example.com", "*", "manage_access")
    assert sessions.resolve_token(hub, token) is None
    assert "already" in run("set-role", "second@example.com", "admin", str(hub))[1]
    assert run("set-role", "second@example.com", "user", str(hub))[0] == 0
    code, out = run("set-role", "first@example.com", "user", str(hub))
    assert code == 1 and "last administrator" in out
    assert run("set-role", "first@example.com", "owner", str(hub))[0] == 2
    waiting = users.create(hub, email="w@example.com", status="pending")
    code, out = run("set-role", "w@example.com", "user", str(hub))
    assert code == 0 and "Approved" in out
    assert users.get(hub, waiting["id"])["status"] == "active"


def test_refuses_open_webui_deployments(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    code, out = run("list", str(hub))
    assert code == 2 and "Open WebUI accounts" in out
