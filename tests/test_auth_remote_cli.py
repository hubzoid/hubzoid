"""The remote CLI (``hubzoid auth``, ``hubzoid api``) against a server, and
``hubzoid auth bootstrap`` keeping the local owner's id."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hubzoid import auth
from hubzoid.auth import routes, sessions, users
from hubzoid.cli import app as cli_app
from hubzoid.control import remote

from tests.test_auth_mfa import hub  # noqa: F401 - fixture
from tests.test_auth_routes import PASSWORD, person

SERVER = "http://localhost:3999"


@pytest.fixture
def server(hub, tmp_path, monkeypatch):
    app = FastAPI()
    routes.mount(app, hub)

    @app.get("/api/agents")
    def agents(request: Request):
        return {"agents": [], "who": auth.require_user(request, hub).email}

    test = TestClient(app, base_url=SERVER)
    original = remote.Client.__init__

    def init(self, server, **kw):
        original(self, server, **kw)
        test.cookies.clear()  # each CLI run is a new process with no cookie jar
        self.http = test

    monkeypatch.setattr(remote.Client, "__init__", init)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HUBZOID_SERVER", SERVER)
    monkeypatch.delenv("HUBZOID_API_KEY", raising=False)
    return app


def run(*args, input_text=None):
    return CliRunner().invoke(cli_app, list(args), input=input_text)


def test_login_whoami_keys_and_api(server, hub):
    person(hub)
    r = run("auth", "login", "--email", "ana@example.com", "--password-stdin", input_text=PASSWORD + "\n")
    assert r.exit_code == 0, r.output
    saved = json.loads(remote.credentials_path().read_text())
    assert saved[SERVER]["email"] == "ana@example.com" and PASSWORD not in remote.credentials_path().read_text()
    assert (remote.credentials_path().stat().st_mode & 0o077) == 0
    who = run("auth", "whoami", "--json")
    assert json.loads(who.output)["user"]["email"] == "ana@example.com"
    made = run("auth", "keys", "create", "ci", "--preset", "read", "--json")
    assert made.exit_code == 0, made.output
    key = json.loads(made.output)["key"]
    listed = run("auth", "keys", "list", "--json")
    assert json.loads(listed.output)["api_keys"][0]["name"] == "ci"
    assert json.loads(run("api", "GET", "/api/agents").output)["who"] == "ana@example.com"
    # With HUBZOID_API_KEY the key signs the call, not the saved session.
    out = CliRunner(env={"HUBZOID_API_KEY": key}).invoke(cli_app, ["api", "GET", "/api/agents"])
    assert json.loads(out.output)["who"] == "ana@example.com"


def test_remote_errors_map_to_exit_codes_and_never_fall_back(server, hub):
    person(hub)
    assert run("api", "GET", "/api/agents").exit_code == 3  # not signed in: no local fallback
    bad = CliRunner(env={"HUBZOID_API_KEY": "hzp_bad_key"}).invoke(cli_app, ["api", "GET", "/api/agents"])
    assert bad.exit_code == 3
    assert run("api", "GET", "agents").exit_code == 2


def test_logout_forgets_the_session(server, hub):
    person(hub)
    run("auth", "login", "--email", "ana@example.com", "--password-stdin", input_text=PASSWORD + "\n")
    assert run("auth", "logout").exit_code == 0
    assert SERVER not in json.loads(remote.credentials_path().read_text() or "{}")
    assert run("auth", "whoami").exit_code == 3


def test_https_is_required_away_from_this_computer():
    with pytest.raises(remote.RemoteError):
        remote.normalize_server("http://team.example.com")
    assert remote.normalize_server("https://team.example.com/") == "https://team.example.com"


def test_bootstrap_keeps_the_local_owners_id(hub, monkeypatch):
    from hubzoid.access import store_for

    monkeypatch.setenv("HUBZOID_AUTH", "false")
    sessions.reset_cache()
    owner = users.ensure_local_owner(hub)
    store_for(hub).grant("admin@localhost", hub.name, "use_hub", actor="test")
    converted = users.convert_local_owner(hub, email="me@example.com", password=PASSWORD, name="Me")
    assert converted["id"] == owner["id"] and converted["email"] == "me@example.com"
    assert store_for(hub).can("me@example.com", hub.name, "use_hub")
    with pytest.raises(users.InvalidAccount):
        users.convert_local_owner(hub, email="other@example.com", password=PASSWORD)
