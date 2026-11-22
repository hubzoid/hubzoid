"""`hubzoid admin config|domain|diagnostics|overview`: validate and export
admin.yaml, read the settings domains, and run a diagnostic report, show it,
bundle it (saved and verified) and delete the bundle, with refusals. No
network, no model."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-SECRET-789")  # must never leak
    return make_hub(tmp_path, monkeypatch)


def admin(*args, as_json=True):
    r = CliRunner().invoke(app, ["admin", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_config_validate_and_export(hub, tmp_path):
    good = tmp_path / "admin.yaml"
    good.write_text("version: 1\ndiagnostics:\n  check_timeout_seconds: 5\n")
    code, out = admin("config", "validate", str(hub), "--file", str(good))
    assert code == 0 and out["valid"] is True and "check_timeout_seconds" not in out["defaulted_keys"]
    assert "bundle_ttl_minutes" in out["defaulted_keys"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ndiagnostics:\n  check_timeout_seconds: 99\n")
    code, out = admin("config", "validate", str(hub), "--file", str(bad))
    assert code == 2 and out["detail"]["code"]

    code, out = admin("config", "export", str(hub), "--domain", "chat")
    assert code == 2 and "Export other domains with their owner" in out["detail"]["message"]
    code, out = admin("config", "export", str(hub), "--format", "bundle")
    assert code == 0 and out["format"] == "bundle" and out["omitted_secret_fields"] == []
    target = tmp_path / "out.yaml"
    code, out = admin("config", "export", str(hub), "--out", str(target))
    exported = target.read_text()
    assert code == 0 and out["file"] == str(target)
    assert exported.startswith("# admin.yaml exported from revision") and "\nversion: 1\n" in exported


def test_domains_overview_and_capabilities(hub):
    code, out = admin("domain", "show", str(hub))
    assert code == 0 and {"chat", "admin"} <= {d["domain"] for d in out["items"]}
    code, one = admin("domain", "show", "admin", str(hub))
    assert code == 0 and one["domain"] == "admin"
    code, out = admin("domain", "show", "no-such", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
    code, out = admin("overview", str(hub), "--days", "3")
    assert code == 0 and "SECRET-789" not in json.dumps(out)
    code, out = admin("diagnostics", "checks", str(hub))
    assert code == 0 and out["items"]


def test_a_diagnostic_report_bundle_cycle(hub, tmp_path):
    code, report = admin("diagnostics", "run", str(hub), "--wait")
    assert report["status"] == "completed" and report["checks"]
    assert code == (1 if report["counts"]["fail"] else 0)
    code, text = admin("diagnostics", "show", report["report_id"], str(hub), as_json=False)
    assert f"report {report['report_id']} completed" in text
    code, listed = admin("diagnostics", "list", str(hub))
    assert report["report_id"] in json.dumps(listed)

    saved = tmp_path / "bundle" / "support.zip"
    code, info = admin("diagnostics", "bundle", report["report_id"], str(hub), "--out", str(saved))
    assert code == 0 and info["saved_to"] == str(saved) and saved.stat().st_size > 0
    assert b"SECRET-789" not in saved.read_bytes()
    code, gone = admin("diagnostics", "bundle-delete", info["bundle_id"], str(hub))
    assert code == 0, gone
    code, out = admin("diagnostics", "show", "no-such-report", str(hub))
    assert code != 0 and out["detail"]["code"]
    code, out = admin("diagnostics", "cancel", report["report_id"], str(hub))
    assert code == 0 and out["status"] == "completed"   # a finished report stays as it was

    code, started = admin("diagnostics", "run", str(hub))
    assert code == 6 and started["report_id"]          # not waited for: accepted and still running
