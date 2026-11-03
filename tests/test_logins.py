"""Login detection, tiers and the choice (hubzoid.logins), with the CLIs and
their login files faked."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest

from hubzoid import factory_claude, factory_codex, logins
from hubzoid.admin import diagnostics as diag
from hubzoid.logins import Login

CODEX_FIX = "cli_auth_credentials_store"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    factory_claude.clear_login_cache()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in ("HUBZOID_CODEX_BIN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    yield
    factory_claude.clear_login_cache()


def fake_claude_status(monkeypatch, out, rc=0):
    calls = []
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, out, "")

    monkeypatch.setattr(factory_claude.subprocess, "run", run)
    return calls


def fake_codex_status(monkeypatch, rc=0, version=None):
    version = version or next(iter(factory_codex.REVIEWED_CODEX_VERSIONS))
    monkeypatch.setattr(factory_codex.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)

    def run(cmd, **kw):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, f"codex-cli {version}\n", "")
        return subprocess.CompletedProcess(cmd, rc, b"", b"")

    monkeypatch.setattr(factory_codex.subprocess, "run", run)


def write_codex_auth(tmp_path, auth_mode="chatgpt", plan="plus"):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    id_token = ".".join([b64({"alg": "none"}), b64({"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}), "x"])
    home = tmp_path / "codex"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": auth_mode, "tokens": {"id_token": id_token}}))


def claude(plan):
    return Login("claude-local", True, "subscription", plan, logins.tier("claude-local", "subscription", plan))


def codex(plan):
    return Login("codex-local", True, "subscription", plan, logins.tier("codex-local", "subscription", plan))


def claude_api_key():
    return Login("claude-local", True, "api_key")


def codex_keychain():
    return Login("codex-local", True, problem="not_file_backed", fix="x")


def test_claude_login_reads_method_and_plan(monkeypatch):
    fake_claude_status(monkeypatch, '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "team"}')
    lg = factory_claude.claude_login()
    assert (lg.signed_in, lg.method, lg.plan, lg.tier) == (True, "subscription", "team", 2)


def test_claude_login_is_cached_and_without_the_cli_is_none(monkeypatch):
    calls = fake_claude_status(monkeypatch, '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}')
    assert factory_claude.claude_login() is factory_claude.claude_login()
    assert len(calls) == 1
    factory_claude.clear_login_cache()
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: None)
    assert factory_claude.claude_login() is None


@pytest.mark.parametrize("rc,out", [(1, ""), (0, "not json"), (0, "{}")])
def test_a_broken_or_old_claude_cli_is_not_signed_in(monkeypatch, rc, out):
    fake_claude_status(monkeypatch, out, rc)
    assert factory_claude.claude_login().signed_in is False


def test_codex_login_reads_the_plan_from_its_login_file(tmp_path, monkeypatch):
    write_codex_auth(tmp_path, auth_mode="chatgpt", plan="plus")
    fake_codex_status(monkeypatch, rc=0)
    lg = factory_codex.codex_login()
    assert (lg.method, lg.plan, lg.tier) == ("subscription", "plus", 1)
    assert "id_token" not in repr(lg)


def test_a_codex_api_key_login_has_no_plan(tmp_path, monkeypatch):
    write_codex_auth(tmp_path, auth_mode="apikey", plan="pro")
    fake_codex_status(monkeypatch)
    lg = factory_codex.codex_login()
    assert (lg.method, lg.plan, lg.tier) == ("api_key", None, 0)


def test_a_hanging_claude_cli_is_not_signed_in(monkeypatch):
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(factory_claude.subprocess, "run", hang)
    factory_claude.clear_login_cache()
    assert factory_claude.claude_login().signed_in is False


def test_a_hanging_codex_login_status_is_not_signed_in(tmp_path, monkeypatch):
    write_codex_auth(tmp_path)
    fake_codex_status(monkeypatch)
    ok = factory_codex.subprocess.run

    def run(cmd, **kw):
        if cmd[1] == "login":
            raise subprocess.TimeoutExpired(cmd, 10)
        return ok(cmd, **kw)

    monkeypatch.setattr(factory_codex.subprocess, "run", run)
    assert factory_codex.codex_login().signed_in is False


@pytest.mark.parametrize("auth_method,method", [
    ("claude.ai", "subscription"), ("oauth_token", "subscription"), ("console", "api_key"),
    ("api_key", "api_key"), ("third_party", "api_key"), ("none", "api_key"),
])
def test_claude_auth_methods(monkeypatch, auth_method, method):
    fake_claude_status(monkeypatch, json.dumps({"loggedIn": True, "authMethod": auth_method, "subscriptionType": "max"}))
    assert factory_claude.claude_login().method == method


@pytest.mark.parametrize("plan,expected", [
    ("promax", 2), ("business", 2), ("self_serve_business_prolite", 2), ("ent26", 2), ("edu_pro", 2),
    ("prolite", 1), ("go", 1), ("edu", 1), ("edu_plus", 1), ("free", 0), ("hc", 0), ("brand_new", 0),
])
def test_codex_plan_tiers(plan, expected):
    assert logins.tier("codex-local", "subscription", plan) == expected


def test_codex_login_without_auth_mode(tmp_path, monkeypatch):
    fake_codex_status(monkeypatch)
    write_codex_auth(tmp_path, plan="pro")           # id_token, no auth_mode
    f = tmp_path / "codex" / "auth.json"
    data = json.loads(f.read_text())
    data.pop("auth_mode")
    f.write_text(json.dumps(data))
    assert factory_codex.codex_login().method == "subscription"
    f.write_text(json.dumps({"OPENAI_API_KEY": "k"}))  # gitleaks:allow
    assert factory_codex.codex_login().method == "api_key"
    f.write_text("{}")
    assert factory_codex.codex_login().method is None


def test_a_codex_keychain_login_names_the_fix(monkeypatch):
    fake_codex_status(monkeypatch, rc=0)          # no auth.json
    lg = factory_codex.codex_login()
    assert lg.problem == "not_file_backed" and CODEX_FIX in lg.fix


def test_codex_not_signed_in_or_not_reviewed(tmp_path, monkeypatch):
    write_codex_auth(tmp_path)
    fake_codex_status(monkeypatch, rc=1)
    assert factory_codex.codex_login().signed_in is False
    fake_codex_status(monkeypatch, version="0.0.1")
    assert factory_codex.codex_login() is None


@pytest.mark.parametrize("plan,expected", [("max", 2), ("pro", 1), ("enterprise", 2), ("free", 0), (None, 0)])
def test_unknown_plans_are_tier_zero(plan, expected):
    assert logins.tier("claude-local", "subscription", plan) == expected
    assert logins.tier("claude-local", "api_key", "max") == 0


@pytest.mark.parametrize("candidates,picked", [
    ([claude("pro"), codex("pro")], "codex-local"),       # higher tier wins
    ([claude("max"), codex("pro")], "claude-local"),      # tie: Claude
    ([claude_api_key(), codex("plus")], "codex-local"),   # subscription over API key
    ([codex_keychain()], None),                            # a problem is not usable
    ([Login("claude-local", False)], None),
    ([], None),
])
def test_choose(candidates, picked):
    got = logins.choose(candidates)
    assert (got.runtime if got else None) == picked


def test_detect_drops_missing_clis_and_describe_reads_plainly(monkeypatch):
    monkeypatch.setattr(factory_claude, "claude_login", lambda: claude("team"))
    monkeypatch.setattr(factory_codex, "codex_login", lambda: None)
    assert logins.detect() == [claude("team")]
    assert logins.describe(claude("team")) == "Claude Code, Team plan"
    assert logins.describe(codex("plus")) == "Codex, Plus plan"
    assert logins.describe(claude_api_key()) == "Claude Code, API key"


# --- doctor ----------------------------------------------------------------
def test_signed_out_claude_fails_even_with_a_claude_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    fake_claude_status(monkeypatch, '{"loggedIn": false}')
    r = diag.check_model_auth(tmp_path)
    assert r.status == "fail" and r.remediation_code == "model_signin"


def test_signed_in_claude_names_the_plan(tmp_path, monkeypatch):
    fake_claude_status(monkeypatch, '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "team"}')
    r = diag.check_model_auth(tmp_path)
    assert r.status == "ok" and "Team plan" in r.summary


def test_an_env_token_passes_without_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")  # gitleaks:allow
    assert diag.check_model_auth(tmp_path).status == "ok"


def test_codex_keychain_login_fails_with_the_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL", "codex-local")
    fake_codex_status(monkeypatch, rc=0)
    r = diag.check_model_auth(tmp_path)
    assert r.status == "fail" and r.remediation_code == "model_signin" and CODEX_FIX in r.remediation


def test_signed_in_codex_names_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL", "codex-local")
    write_codex_auth(tmp_path, plan="plus")
    fake_codex_status(monkeypatch)
    r = diag.check_model_auth(tmp_path)
    assert r.status == "ok" and "Plus plan" in r.summary
