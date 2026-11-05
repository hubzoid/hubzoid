"""Login detection, tiers and the choice (hubzoid.logins), with the CLIs and
their login files faked."""
from __future__ import annotations

import base64
import json
import os
import subprocess

import pytest

from hubzoid import factory_claude, factory_codex, logins
from hubzoid.admin import diagnostics as diag
from hubzoid.logins import Login

CODEX_FIX = "cli_auth_credentials_store"

# These test the probes and the check call themselves, with the CLIs faked here.
pytestmark = pytest.mark.real_logins


def _only_run(run):
    """A stand-in for the `subprocess` a module sees, so no other module gets the fake."""
    import subprocess as real
    import types
    return types.SimpleNamespace(**{**vars(real), "run": run})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    factory_claude.clear_login_cache()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in ("HUBZOID_CODEX_BIN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_BASE_URL", "MODEL"):
        monkeypatch.delenv(name, raising=False)
    yield
    factory_claude.clear_login_cache()


def fake_claude_status(monkeypatch, out, rc=0):
    calls = []
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, out, "")

    monkeypatch.setattr(factory_claude, "subprocess", _only_run(run))
    return calls


def fake_codex_status(monkeypatch, rc=0, version=None):
    version = version or next(iter(factory_codex.REVIEWED_CODEX_VERSIONS))
    monkeypatch.setattr(factory_codex.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)

    def run(cmd, **kw):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, f"codex-cli {version}\n", "")
        return subprocess.CompletedProcess(cmd, rc, b"", b"")

    monkeypatch.setattr(factory_codex, "subprocess", _only_run(run))


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
    calls = fake_claude_status(monkeypatch, out, rc)
    assert factory_claude.claude_login().signed_in is False
    factory_claude.claude_login()
    assert len(calls) == 2               # no answer is not cached: the next start asks again


def test_a_clean_not_signed_in_answer_is_cached(monkeypatch):
    calls = fake_claude_status(monkeypatch, '{"loggedIn": false}', rc=1)
    assert factory_claude.claude_login().signed_in is False
    factory_claude.claude_login()
    assert len(calls) == 1


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

    monkeypatch.setattr(factory_claude, "subprocess", _only_run(hang))
    factory_claude.clear_login_cache()
    assert factory_claude.claude_login().signed_in is False
    assert factory_claude._login_cache is None   # a timeout is never cached


def test_a_hanging_codex_login_status_is_not_signed_in(tmp_path, monkeypatch):
    write_codex_auth(tmp_path)
    fake_codex_status(monkeypatch)
    ok = factory_codex.subprocess.run

    def run(cmd, **kw):
        if cmd[1] == "login":
            raise subprocess.TimeoutExpired(cmd, 10)
        return ok(cmd, **kw)

    monkeypatch.setattr(factory_codex, "subprocess", _only_run(run))
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
    assert logins.describe(codex("self_serve_business_prolite")) == "Codex, Self-serve Business Pro Lite plan"
    assert logins.describe(codex("ent26")) == "Codex, Enterprise plan"
    assert logins.describe(claude("brand_new_tier")) == "Claude Code, Brand New Tier plan"


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


# -- billing: a subscription login beats an inherited API key; one real check call --
def _billing_env(monkeypatch, *, login=None, token=False):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    monkeypatch.setattr(factory_claude, "claude_login", lambda: login)
    if token:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")  # gitleaks:allow


def test_billing_subscription_login_blanks_the_inherited_api_key(monkeypatch):
    _billing_env(monkeypatch, login=Login("claude-local", True, "subscription", "max"))
    env = factory_claude._child_env()
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_billing_api_key_passes_without_a_subscription(monkeypatch):
    _billing_env(monkeypatch, login=None)
    assert "ANTHROPIC_API_KEY" not in factory_claude._child_env()
    _billing_env(monkeypatch, login=Login("claude-local", True, "api_key"))
    assert "ANTHROPIC_API_KEY" not in factory_claude._child_env()


def test_billing_oauth_token_blanks_the_key(monkeypatch):
    _billing_env(monkeypatch, login=None, token=True)
    assert factory_claude._child_env()["ANTHROPIC_API_KEY"] == ""


def test_billing_an_anthropic_model_keeps_its_key(monkeypatch):
    _billing_env(monkeypatch, login=Login("claude-local", True, "subscription", "max"))
    assert "ANTHROPIC_API_KEY" not in factory_claude._child_env("anthropic/claude-haiku-4-5")


def test_billing_a_gateway_base_url_keeps_its_key(monkeypatch):
    _billing_env(monkeypatch, login=Login("claude-local", True, "subscription", "max"), token=True)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.test")
    env = factory_claude._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_billing_logs_only_a_key_it_blanked_and_why(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(factory_claude, "_billing_logged", False)
    monkeypatch.setattr(factory_claude, "claude_login", lambda: Login("claude-local", True, "subscription", "max"))
    caplog.set_level(logging.WARNING, logger="hubzoid.claude")  # a warning: INFO never reaches the hub log
    factory_claude._child_env()                                  # no key to ignore: nothing said
    assert "ignoring" not in caplog.text
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-secret-value")  # gitleaks:allow
    factory_claude._child_env()
    factory_claude._child_env()                                  # once per process
    assert "ignoring ANTHROPIC_API_KEY" in caplog.text and "claude login" in caplog.text
    assert caplog.text.count("ignoring") == 1 and "inherited-secret-value" not in caplog.text
    assert all(r.levelno == logging.WARNING for r in caplog.records if "ignoring" in r.getMessage())
    monkeypatch.setattr(factory_claude, "_billing_logged", False)
    caplog.clear()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")  # gitleaks:allow
    factory_claude._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in caplog.text


def test_model_reply_check_maps_ok_and_fail(tmp_path, monkeypatch):
    from hubzoid import runtime

    monkeypatch.setattr(runtime, "reply_check", lambda hub, **kw: (True, ""))
    assert diag.BY_ID["model.reply"].fn(tmp_path).status == "ok"
    monkeypatch.setattr(runtime, "reply_check", lambda hub, **kw: (False, "timed out"))
    r = diag.BY_ID["model.reply"].fn(tmp_path)
    assert r.status == "fail" and "timed out" in r.summary
    assert diag.BY_ID["model.reply"].profile == "active"


def test_reply_check_wraps_complete_once(tmp_path, monkeypatch):
    from hubzoid import runtime

    seen = {}

    def once(hub, spec, **kw):
        seen.update(spec=spec, kw=kw)
        return {"text": "ready"}

    monkeypatch.setattr(runtime, "complete_once", once)
    assert runtime.reply_check(tmp_path, timeout=5) == (True, "")
    assert seen["spec"]["timeout"] == 5 and seen["kw"]["kind"] == "check"
    monkeypatch.setattr(runtime, "complete_once", lambda *a, **k: {"text": " "})
    assert runtime.reply_check(tmp_path)[0] is False

    def boom(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(runtime, "complete_once", boom)
    ok, reason = runtime.reply_check(tmp_path)
    assert not ok and "slow" in reason


# -- the probe must not see an inherited key (it would report api_key and hide the login) --
def _fake_cli_by_env(monkeypatch, stored="claude.ai"):
    """A claude CLI that, like the real one, reports api_key whenever a key is in its env."""
    seen = []
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)

    def run(cmd, **kw):
        env = kw.get("env") if kw.get("env") is not None else os.environ
        seen.append(dict(env))
        if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
            out = '{"loggedIn": true, "authMethod": "api_key", "subscriptionType": null}'
        elif stored:
            out = '{"loggedIn": true, "authMethod": "%s", "subscriptionType": "team"}' % stored
        else:
            out = '{"loggedIn": false}'
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(factory_claude, "subprocess", _only_run(run))
    return seen


def test_probe_ignores_an_inherited_key_and_sees_the_subscription(monkeypatch):
    seen = _fake_cli_by_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited")  # gitleaks:allow
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy")  # gitleaks:allow
    login = factory_claude.claude_login()
    assert (login.method, login.plan, login.api_key_in_env) == ("subscription", "team", True)
    assert "ANTHROPIC_API_KEY" not in seen[0] and "ANTHROPIC_AUTH_TOKEN" not in seen[0]
    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy"        # a plan login stays
    assert os.environ["ANTHROPIC_API_KEY"] == "inherited"       # our own environment is untouched
    assert factory_claude._child_env("claude-local")["ANTHROPIC_API_KEY"] == ""


def test_probe_flags_no_key_when_none_is_set(monkeypatch):
    _fake_cli_by_env(monkeypatch)
    assert factory_claude.claude_login().api_key_in_env is False


def test_a_gateway_base_url_keeps_the_key_even_with_a_login(monkeypatch):
    seen = _fake_cli_by_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.test")
    assert factory_claude.claude_login().method == "api_key"    # the probe sees what the child uses
    assert seen[0]["ANTHROPIC_API_KEY"] == "inherited"
    assert "ANTHROPIC_API_KEY" not in factory_claude._child_env("claude-local")


def test_a_probe_with_no_answer_warns_once_that_the_key_is_used(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-inherited")  # gitleaks:allow
    monkeypatch.setattr(factory_claude.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(factory_claude, "subprocess", _only_run(slow))
    with caplog.at_level(logging.WARNING, logger="hubzoid.claude"):
        assert factory_claude.claude_login().signed_in is False         # no change: not signed in
        factory_claude.claude_login()
        factory_claude.clear_login_cache()
        monkeypatch.setattr(factory_claude.shutil, "which", lambda name: None)
        assert factory_claude.claude_login() is None
        factory_claude.clear_login_cache()
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.test")
        factory_claude.claude_login()                                   # a gateway uses the key on purpose
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and "timed out" in warned[0] and "no `claude`" in warned[1]
    assert all("ANTHROPIC_API_KEY in the environment will be used" in w for w in warned)
    assert "fake-inherited" not in caplog.text


def test_only_a_key_and_no_login_keeps_the_key(tmp_path, monkeypatch):
    _fake_cli_by_env(monkeypatch, stored=None)                  # without the key: loggedIn false
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    login = factory_claude.claude_login()
    assert (login.signed_in, login.method, login.tier, login.api_key_in_env) == (True, "api_key", 0, True)
    assert "ANTHROPIC_API_KEY" not in factory_claude._child_env("claude-local")
    from hubzoid import doctor

    monkeypatch.setenv("MODEL", "claude-local")
    check = doctor._model(tmp_path)
    assert check.status == "ok" and "ignored" not in check.summary


def test_tool_free_options_decide_by_the_call_model_not_the_shell_model(monkeypatch):
    _fake_cli_by_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    monkeypatch.setenv("MODEL", "openrouter/x")
    assert factory_claude.tool_free_options("s", model="claude-local").env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_API_KEY" not in (factory_claude.tool_free_options("s", model="anthropic/claude-haiku-4-5").env or {})


def test_doctor_says_an_env_key_is_ignored_when_it_is(tmp_path, monkeypatch):
    from hubzoid import doctor

    _fake_cli_by_env(monkeypatch)
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")  # gitleaks:allow
    note = "An ANTHROPIC_API_KEY in the environment is ignored."
    assert diag.check_model_auth(tmp_path).summary == f"Signed in: Claude Code, Team plan. {note}"
    check = doctor._model(tmp_path)
    assert check.status == "ok" and check.summary == f"claude-local: signed in (Claude Code, Team plan). {note}"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited")  # gitleaks:allow
    assert diag.check_model_auth(tmp_path).summary.endswith(
        "ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN in the environment are ignored.")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert diag.check_model_auth(tmp_path).summary.endswith("An ANTHROPIC_AUTH_TOKEN in the environment is ignored.")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.test")
    assert "ignored" not in diag.check_model_auth(tmp_path).summary
    assert "ignored" not in doctor._model(tmp_path).summary
