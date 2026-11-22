"""Hosted account CLI request bodies, MFA challenges and safe error output."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from hubzoid.auth import remote_cli as cli
from hubzoid.control import remote
from tests.test_auth_remote_cli import run, server  # noqa: F401
from tests.test_auth_mfa import hub  # noqa: F401
from tests.test_auth_routes import PASSWORD, person


def test_human_outputs_and_security_commands(server, hub):
    person(hub)
    from tests.access_helpers import allow
    allow(hub, "ana@example.com")
    assert run("auth", "login", "--email", "ana@example.com", "--password-stdin", input_text=PASSWORD + "\n").exit_code == 0
    assert "ana@example.com" in run("auth", "whoami").output
    key = json.loads(run("auth", "keys", "create", "reader", "--operations", "chat", "--agent", hub.name, "--json").output)
    assert key["api_key"]["operations"] == ["chat"]
    assert key["api_key"]["hubs"] == [hub.name]
    assert "reader" in run("auth", "keys", "list").output
    assert run("auth", "keys", "revoke", key["api_key"]["id"]).exit_code == 0
    assert "Two-step sign-in is off" in run("auth", "mfa", "status").output
    assert json.loads(run("auth", "mfa", "status", "--json").output)["mfa_required"] is False
    devices = json.loads(run("auth", "sessions", "list", "--json").output)["sessions"]
    assert len(devices) == 1
    assert devices[0]["id"] in run("auth", "sessions", "list").output
    assert "0 other device" in run("auth", "sessions", "revoke-others").output
    assert run("auth", "sessions", "revoke", devices[0]["id"]).exit_code == 0
    assert "Not signed in" in run("auth", "whoami").output


def test_api_body_file_and_invalid_input(server, hub, tmp_path):
    person(hub)
    from tests.access_helpers import allow
    allow(hub, "ana@example.com")
    assert run("auth", "login", "--email", "ana@example.com", "--password-stdin", input_text=PASSWORD + "\n").exit_code == 0
    path = tmp_path / "request.json"
    path.write_text('{"name":"script", "preset":"read"}')
    r = run("api", "POST", "/api/me/api-keys", "--data", "@" + str(path))
    assert r.exit_code == 0 and json.loads(r.output)["api_key"]["name"] == "script"
    r = run("api", "POST", "/api/me/api-keys", "--data", "bad")
    assert r.exit_code == 2 and "must be JSON" in r.output


@pytest.mark.parametrize("state,code,exit_code,message", [("mfa_setup", "", 3, "browser"),
    ("signed_in", "", 1, "did not start a session"),
    ("mfa_required", "123456", 0, "Signed in"),
    ("mfa_required", "recovery-code", 0, "Signed in")])
def test_login_challenge_flow(monkeypatch, state, code, exit_code, message):
    calls, saved = [], []
    class Client:
        server, api_key = "https://team.example.com", "hzp_present"
        def request(self, method, path, body, **kw):
            calls.append((path, body, kw))
            if path.endswith("login"):
                return httpx.Response(200, json={"state": state}, request=httpx.Request(method, "https://team.example.com" + path), headers={"set-cookie": "step=nonce"})
            return httpx.Response(200, json={"state": "signed_in"}, request=httpx.Request(method, "https://team.example.com" + path),
                                  headers={"set-cookie": remote.SESSION_COOKIE + "=session-token"})
    monkeypatch.setattr(cli, "_client", lambda server: Client())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: code)
    monkeypatch.setattr(remote, "save_session", lambda *args: saved.append(args))
    r = CliRunner().invoke(cli.auth_app, ["login", "--email", "ana@example.com", "--password-stdin"], input="private-password\n")
    assert r.exit_code == exit_code and message in r.output and "private-password" not in r.output
    assert calls[0][1] == {"email": "ana@example.com", "password": "private-password"}
    if state == "mfa_required":
        field = "code" if code.isdigit() else "recovery_code"
        assert calls[1] == ("/api/auth/mfa/verify", {field: code}, {"cookies": {"step": "nonce"}})
        assert saved == [("https://team.example.com", "session-token", "ana@example.com")]
    else:
        assert saved == []


def test_remote_failures_and_missing_server(monkeypatch):
    monkeypatch.setattr(remote, "server_from", lambda server: None)
    r = CliRunner().invoke(cli.auth_app, ["whoami"])
    assert r.exit_code == 2 and "Name the deployment" in r.output
    def broken(server):
        raise remote.RemoteError(2, "invalid URL")
    monkeypatch.setattr(remote, "server_from", broken)
    r = CliRunner().invoke(cli.auth_app, ["whoami"])
    assert r.exit_code == 2 and "invalid URL" in r.output
    class Client:
        def call(self, *a):
            raise remote.RemoteError(3, "denied", body={"detail": {"code": "denied"}})
    monkeypatch.setattr(cli, "_client", lambda server: Client())
    r = CliRunner().invoke(cli.auth_app, ["whoami", "--json"])
    assert r.exit_code == 3 and json.loads(r.output)["detail"]["code"] == "denied"


def test_policy_requests_and_human_key_output(monkeypatch):
    calls = []
    def call(server, method, path, body=None, as_json=False):
        calls.append((method, path, body))
        if path.endswith("api-keys"):
            return {"api_key": {"name": "ci"}, "key": "hzp_shown_once"}
        return {"policy": "admins"}
    monkeypatch.setattr(cli, "_run", call)
    r = CliRunner().invoke(cli.auth_app, ["mfa", "policy"])
    assert r.exit_code == 0 and "admins" in r.output
    r = CliRunner().invoke(cli.auth_app, ["mfa", "policy", "admins"])
    assert r.exit_code == 0 and calls[-1] == ("PUT", "/api/auth/mfa/policy", {"policy": "admins"})
    r = CliRunner().invoke(cli.auth_app, ["keys", "create", "ci"])
    assert r.exit_code == 0 and "hzp_shown_once" in r.output


def test_logout_forgets_even_if_server_is_unreachable(monkeypatch):
    forgotten = []
    def broken(*a):
        raise remote.RemoteError(5, "unavailable")
    monkeypatch.setattr(cli, "_client", lambda server: SimpleNamespace(
        server="https://team.example.com", session="token", request=broken))
    monkeypatch.setattr(remote, "forget", forgotten.append)
    r = CliRunner().invoke(cli.auth_app, ["logout"])
    assert r.exit_code == 0 and forgotten == ["https://team.example.com"]
