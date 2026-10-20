"""Regressions for the GitZoid release follow-up (no provider calls)."""
from __future__ import annotations

import shutil
import random
import string

import pytest

from tests import gitzoid_harness as h
from tests.test_gitzoid_workflows import REPO, store


def changed(name, text):
    lines = text.splitlines()
    return {"filename": name, "status": "added", "patch": f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)}


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks binary not installed")
@pytest.mark.parametrize("suppression", ["config", "ignore", "inline"])
def test_gitleaks_cannot_load_pr_config_and_still_scans_dotfiles(suppression):
    gz = h.template_lib()
    # Construct a synthetic token so this regression fixture isn't itself flagged.
    token = "ghp_" + "".join(random.Random(42).choices(string.ascii_letters + string.digits, k=36))
    contents = {".env": 'TOKEN="' + token + '"' + (" # gitleaks:allow" if suppression == "inline" else "") + "\n"}
    if suppression == "config":
        contents[".gitleaks.toml"] = '[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n'
    if suppression == "ignore":
        contents[".gitleaksignore"] = ".env:github-pat:1\n"
    a = {"name": "gitleaks", "run": ["gitleaks", "dir", ".", "--report-format", "sarif", "--report-path", "-", "--no-banner", "--exit-code", "0", "--redact"]}
    checks, notes = gz.checks.run_analyzers([changed(p, v) for p, v in contents.items()], contents.get, [a])
    assert any(c["path"] == ".env" and c["line"] == 1 for c in checks), notes
    assert all(token not in c["message"] for c in checks)


@pytest.mark.parametrize("command", ["custom-sarif", "/custom/bin/ruff", "bin/gitleaks"])
def test_custom_analyzer_requires_explicit_project_code_opt_in(monkeypatch, command):
    gz = h.template_lib()
    calls = []
    monkeypatch.setattr(gz.checks.shutil, "which", lambda cmd: "/trusted/" + cmd)
    def run(cmd, **kwargs):
        calls.append(cmd)
        from types import SimpleNamespace
        return SimpleNamespace(stdout='{"runs": []}', returncode=0)
    monkeypatch.setattr(gz.checks.subprocess, "run", run)
    args = gz.checks.SAFE_RUNS.get(command.rsplit("/", 1)[-1], ["."])
    a = {"name": "custom", "run": [command, *args]}
    _, notes = gz.checks.run_analyzers([], lambda p: "", [a])
    assert not calls and "allow_project_code" in notes[0]
    gz.checks.run_analyzers([], lambda p: "", [{**a, "allow_project_code": True}])
    assert len(calls) == 1


def test_audit_oversized_line_does_not_starve_later_files(tmp_path, monkeypatch):
    h.isolate(monkeypatch)
    hub = h.make_hub(tmp_path)
    gz = h.lib(hub)
    root = tmp_path / "checkout"
    root.mkdir()
    # One oversized line in an otherwise ordinary file (a whole minified file is skipped instead).
    (root / "auth.py").write_text("x" * (gz.audit.BATCH_CHARS + 1) + "\n" + "ok = True\n" * 600)
    (root / "b.py").write_text("print('second file')\n")
    (root / "c.py").write_text("print('third file')\n")
    head = {"sha": "same-commit"}
    monkeypatch.setattr(gz.checkout, "ensure", lambda *a: (root, head["sha"]))
    monkeypatch.setattr(gz.checkout, "path_for", lambda *a: root)
    monkeypatch.setattr(gz.checkout, "current_sha", lambda *a: head["sha"])
    h.write_settings(hub, {"audit": {"max_files_per_repo": 1}})
    run = h.wf(hub, "deep_audit")
    for _ in range(4):  # three readable windows, one per run, then nothing left to read
        result = h.run_local(run, hub_dir=hub, trigger="operator")
        assert result.value["unfinished"] and not store(hub).audit(REPO)["last_full"]
        assert "oversized" in " ".join(x["error"] for x in result.value["unfinished"])
    units = [gz.audit.unit(u) for u in store(hub).audit(REPO)["done"]]
    assert {u[0] for u in units} == {"auth.py", "b.py", "c.py"}
    assert ("auth.py", 0) not in {(u[0], u[1]) for u in units}  # the oversized line was never read
    assert not result.calls  # no repeated model calls once the readable units are done
    assert "oversized" in store(hub).coverage([REPO])[0]["last_error"]
    # A new commit can repair the line and complete a fresh audit.
    (root / "auth.py").write_text("print('repaired')\n")
    head["sha"] = "new-commit"
    for _ in range(3):
        h.run_local(run, hub_dir=hub, trigger="operator")
    assert store(hub).audit(REPO)["last_full"]


@pytest.mark.parametrize("model,key", [("openai/test", "OPENAI_API_KEY"), ("openrouter/anthropic/test", "OPENROUTER_API_KEY"), ("anthropic/test", "ANTHROPIC_API_KEY")])
def test_e2e_prerequisites_follow_selected_provider(monkeypatch, model, key):
    from tests.e2e import test_gitzoid_e2e as e2e
    monkeypatch.setattr(e2e.shutil, "which", lambda name: None)
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert key in e2e.prerequisite_problem(model)
    monkeypatch.setenv(key, "synthetic-test-value")
    assert e2e.prerequisite_problem(model) is None
    assert "claude" in e2e.prerequisite_problem("claude-local/sonnet")
    assert "codex" in e2e.prerequisite_problem("codex-local")


def test_ruff_preset_disables_project_ignores(monkeypatch):
    gz = h.template_lib()
    monkeypatch.setattr(gz.checks.shutil, "which", lambda cmd: "/trusted/ruff")
    def run(cmd, **kwargs):
        from pathlib import Path
        from types import SimpleNamespace
        target = Path(cmd[cmd.index("F,B,S") + 1])
        assert target != kwargs["cwd"] and (target / ".gitignore").is_file()
        assert "--isolated" in cmd and "--ignore-noqa" in cmd and "--no-respect-gitignore" in cmd
        assert not (Path(kwargs["env"]["HOME"]) / ".config").exists()
        return SimpleNamespace(stdout='{"runs": []}', returncode=0)
    monkeypatch.setattr(gz.checks.subprocess, "run", run)
    a = {"name": "ruff", "run": ["ruff", *gz.checks.SAFE_RUNS["ruff"]]}
    _, notes = gz.checks.run_analyzers([changed(".gitignore", "*.py")], lambda p: "*.py", [a])
    assert notes == ["ruff: 0 result(s) on added lines"]


def test_audit_changed_checkout_stops_before_model_calls(tmp_path, monkeypatch):
    h.isolate(monkeypatch)
    hub = h.make_hub(tmp_path)
    gz = h.lib(hub)
    monkeypatch.setattr(gz.checkout, "current_sha", lambda *a: "different-commit")
    result = h.run_local(h.wf(hub, "deep_audit"), hub_dir=hub, trigger="operator")
    assert not result.calls and not store(hub).audit(REPO)["done"]
    assert "checkout changed" in result.value["unfinished"][0]["error"]


def test_e2e_azure_checks_endpoint_and_key(monkeypatch):
    from tests.e2e import test_gitzoid_e2e as e2e
    monkeypatch.setenv("AZURE_API_KEY", "synthetic-test-value")
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    assert "AZURE_API_BASE" in e2e.prerequisite_problem("azure/test")
    monkeypatch.setenv("AZURE_API_BASE", "https://example.invalid")
    assert e2e.prerequisite_problem("azure/test") is None


def test_audit_keeps_progress_across_new_commits_and_progress_is_not_a_failure(tmp_path, monkeypatch):
    h.isolate(monkeypatch)
    hub = h.make_hub(tmp_path)
    gz = h.lib(hub)
    root = tmp_path / "checkout"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text(f"print('{name}')\n")
    head = {"sha": "one"}
    monkeypatch.setattr(gz.checkout, "ensure", lambda *a: (root, head["sha"]))
    monkeypatch.setattr(gz.checkout, "path_for", lambda *a: root)
    monkeypatch.setattr(gz.checkout, "current_sha", lambda *a: head["sha"])
    h.write_settings(hub, {"audit": {"max_files_per_repo": 1}})
    run = h.wf(hub, "deep_audit")
    first = h.run_local(run, hub_dir=hub, trigger="operator")
    assert first.value["unfinished"][0]["in_progress"]
    assert not any(c["last_error"] for c in store(hub).coverage([REPO]))      # in progress is not a failure
    assert all("could not run" not in e["subject"] for e in first.emails)
    head["sha"] = "two"                                                       # main moved on overnight
    (root / "b.py").write_text("print('changed')\n")
    h.run_local(run, hub_dir=hub, trigger="operator")
    assert len(store(hub).audit(REPO)["done"]) == 2                           # a.py was not read again
    last = h.run_local(run, hub_dir=hub, trigger="operator")
    assert store(hub).audit(REPO)["last_full"] and not last.value["unfinished"]
