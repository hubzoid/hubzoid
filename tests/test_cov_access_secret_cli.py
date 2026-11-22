"""Secret CLI validation, dry runs, write-only values and failure reporting."""
from __future__ import annotations

import io
import json

import pytest
from typer.testing import CliRunner

from hubzoid.secretstore import cli, store
from tests.test_secretstore import hub, isolated_env, put  # noqa: F401


def invoke(hub, command, *args, **kw):
    return CliRunner().invoke(cli.secrets_app, [command, str(hub), *args], **kw)


@pytest.mark.parametrize("args,code,message", [
    ([], "invalid_request", "Name the secret"),
    (["--name", "token", "--ref", "secret:deployment/token"], "invalid_request", "not both"),
    (["--ref", "bad"], "invalid_ref", ""),
    (["--ref", "env:TOKEN"], "invalid_request", "env: values"),
    (["--ref", "secret:hub/elsewhere/token"], "hub_not_found", "another hub"),
    (["--name", "token", "--restricted", "--deployment"], "invalid_request", "cannot be restricted"),
    (["--name", "token"], "operator_source", ".env"),
    (["--ref", "secret:channel/deskhub/mail/../token"], "invalid_ref", ""),
])
def test_set_refusals(hub, args, code, message):
    r = invoke(hub, "set", *args, "--json")
    assert r.exit_code != 0, r.output
    detail = json.loads(r.output)["detail"]
    assert detail["code"] == code and message in detail["message"]


def test_invalid_hub_and_list_error(hub, monkeypatch):
    r = invoke(hub / "absent", "list", "--json")
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "invalid_request"
    from hubzoid.secretstore import report
    def broken(*a, **kw):
        raise RuntimeError("private-value")
    monkeypatch.setattr(report, "rows", broken)
    r = invoke(hub, "list", "--json")
    assert r.exit_code == 5 and "RuntimeError" in r.output and "private-value" not in r.output


def test_human_list_notes_and_json(hub, monkeypatch):
    from hubzoid.secretstore import report
    def row(status, stored, expected=""):
        return {"name": "TOKEN_" + status.upper(), "level": "hub", "status": status,
                "stored_status": stored, "expected": expected, "source": "test"}
    rows = [row("set", "overridden"), row("blank", "unreadable"), row("missing", "rebind", "channel")]
    monkeypatch.setattr(report, "rows", lambda *a, **kw: rows)
    r = invoke(hub, "list")
    assert r.exit_code == 0 and "stored value unused" in r.output and "empty value" in r.output
    assert "needed (channel)" in r.output and "set it again" in r.output
    r = invoke(hub, "list", "--json", "--skip-secret-fetch")
    assert json.loads(r.output)["secrets"] == rows
    monkeypatch.setattr(report, "rows", lambda *a, **kw: [])
    assert "No secrets set or expected" in invoke(hub, "list").output


def test_channel_dry_run_save_replace_delete(hub):
    ref = "secret:channel/deskhub/mail/password"
    r = invoke(hub, "set", "--ref", ref, "--check", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["would"] == "create"
    r = invoke(hub, "set", "--ref", ref, "--stdin", "--json", input="private-value\n")
    assert r.exit_code == 0 and json.loads(r.output)["secret"]["revision"] == 1
    assert "private-value" not in r.output
    assert "Would replace" in invoke(hub, "set", "--ref", ref, "--check").output
    r = invoke(hub, "set", "--ref", ref, "--stdin", "--expected-revision", "1", input="new-value")
    assert r.exit_code == 0 and "revision 2" in r.output and "new-value" not in r.output
    r = invoke(hub, "delete", "--ref", ref, "--check", "--json")
    assert json.loads(r.output)["current_revision"] == 2
    assert "Would delete" in invoke(hub, "delete", "--ref", ref, "--check").output
    r = invoke(hub, "delete", "--ref", ref, "--expected-revision", "1", "--json")
    assert r.exit_code == 4 and json.loads(r.output)["detail"]["code"] == "revision_conflict"
    r = invoke(hub, "delete", "--ref", ref, "--json")
    assert r.exit_code == 0 and json.loads(r.output)["restart_required"] is True
    assert store.get_meta(hub, scope="channel", target="deskhub/mail", name="password") is None
    assert invoke(hub, "delete", "--ref", ref, "--check", "--json").exit_code == 1


@pytest.mark.parametrize("revision", ["absent", "nonsense"])
def test_delete_invalid_revision(hub, revision):
    r = invoke(hub, "delete", "--name", "TOKEN", "--expected-revision", revision, "--json")
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "validation_failed"


def test_channel_set_failures_do_not_echo_values(hub, monkeypatch):
    ref = "secret:channel/deskhub/mail/password"
    assert invoke(hub, "set", "--ref", ref, "--json").exit_code == 2
    def broken(*a, **kw):
        raise RuntimeError("private-value")
    monkeypatch.setattr(store, "put", broken)
    r = invoke(hub, "set", "--ref", ref, "--stdin", "--json", input="private-value")
    assert r.exit_code == 5 and "private-value" not in r.output
    monkeypatch.setattr(store, "delete", broken)
    r = invoke(hub, "delete", "--ref", ref, "--json")
    assert r.exit_code == 5 and "private-value" not in r.output


@pytest.mark.parametrize("raw,wanted", [("abc\r\n", "abc"), ("abc\n", "abc"), ("abc", "abc")])
def test_stdin_removes_only_one_line_ending(monkeypatch, raw, wanted):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(raw))
    assert cli._read_value(True, True) == wanted


def test_hidden_prompt_and_actor_fallback(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", type("Terminal", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "hidden")
    assert cli._read_value(False, False) == "hidden"
    def broken():
        raise OSError("no login")
    monkeypatch.setattr(cli.getpass, "getuser", broken)
    assert cli._actor() == "cli"


def test_new_key_is_a_valid_fernet_key():
    from cryptography.fernet import Fernet
    r = CliRunner().invoke(cli.secrets_app, ["new-key"])
    assert r.exit_code == 0
    key = Fernet(r.output.strip().encode())
    assert key.decrypt(key.encrypt(b"payload")) == b"payload"
