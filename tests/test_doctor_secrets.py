"""`hubzoid doctor`: the configuration layer report (names and sources only),
secret reachability (skippable, so no network in tests), misplaced secret
names, and Google sign-in without merge-by-email."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid import config_secrets as cs
from hubzoid import doctor as doc
from tests._fake_secrets import clean_process_env, install

SENTINEL = "doctor-must-never-print-this"


@pytest.fixture
def hub(tmp_path):
    h = tmp_path / "hub"
    (h / "restricted").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: hub\ndescription: d\nmodel: openrouter/anthropic/claude-haiku-4.5\n---\nbody")
    (h / ".env").write_text(f"BRIDGE_API_KEYS=a-properly-long-random-key\nPLAIN={SENTINEL}\n"
                            "HUBZOID_HUB_SECRET_NAME=hubsec\nAWS_REGION=eu-west-1\n")
    (h / "restricted" / ".env").write_text(f"TOOL_TOKEN={SENTINEL}\n")
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    with clean_process_env(OPENROUTER_API_KEY="sk-test"):
        yield h


def _by_id(checks):
    return {c.id: c for c in checks}


def test_layer_report_and_readable_secret(hub, monkeypatch):
    install(monkeypatch, {"hubsec": {"FROM_SECRET": SENTINEL, "PLAIN": SENTINEL}})
    checks = _by_id(doc.run(hub))
    assert checks["secrets.hub"].status == "ok" and "2 key(s)" in checks["secrets.hub"].summary
    rows = {r["key"]: r for r in checks["config.layers"].detail}
    assert rows["PLAIN"]["layer"] == "hub secret" and rows["PLAIN"]["shadows"] == ["hub"]
    assert rows["TOOL_TOKEN"]["layer"] == "restricted"
    assert rows["FROM_SECRET"]["source"] == "aws:hubsec"
    assert SENTINEL not in json.dumps(doc.report(hub, list(checks.values())), default=str)


def test_unreadable_secret_fails_and_the_rest_still_runs(hub, monkeypatch):
    install(monkeypatch, {})
    checks = _by_id(doc.run(hub))
    assert checks["secrets.hub"].status == "fail"
    assert "ResourceNotFoundException" in checks["secrets.hub"].summary
    assert checks["auth.bridge_keys"].status == "ok"  # the hub still loaded from its files
    assert "runtime.build" in checks


def test_skip_secret_fetch_never_calls_aws(hub, monkeypatch):
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("--skip-secret-fetch"))
    checks = _by_id(doc.run(hub, fetch_secrets=False))
    assert checks["secrets.hub"].status == "info" and "was not read" in checks["secrets.hub"].summary
    result = CliRunner().invoke(cli.app, ["doctor", str(hub), "--skip-secret-fetch", "--json"])
    assert SENTINEL not in result.output
    ids = {c["id"] for c in json.loads(result.output)["checks"]}
    assert {"secrets.hub", "config.layers"} <= ids


def test_console_output_lists_names_and_sources_only(hub, monkeypatch):
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("--skip-secret-fetch"))
    result = CliRunner().invoke(cli.app, ["doctor", str(hub), "--skip-secret-fetch"])
    assert "PLAIN: hub" in result.output and "TOOL_TOKEN: restricted" in result.output
    assert SENTINEL not in result.output


def test_misplaced_secret_names_warn(hub, monkeypatch):
    install(monkeypatch, {"hubsec": {}})
    os.environ["HUBZOID_RESTRICTED_SECRET_NAME"] = "from-env"
    checks = _by_id(doc.run(hub))
    assert checks["secrets.names"].status == "warn"
    assert any("HUBZOID_RESTRICTED_SECRET_NAME" in line for line in checks["secrets.names"].detail)


def test_gateway_hub_naming_aws_secret_name_warns(hub, monkeypatch, tmp_path):
    from hubzoid import deployment

    deployment.save(tmp_path / "gw" / "deployment.json",
                    hubs=[dict(key="hub", name="hub", path=str(hub), model_id="hub")],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_url="http://127.0.0.1:1",
                    owui_db=str(tmp_path / "webui.db"))
    with (hub / ".env").open("a") as f:
        f.write("AWS_SECRET_NAME=redirect\n")
    fake = install(monkeypatch, {"hubsec": {}})
    checks = _by_id(doc.run(hub))
    assert "redirect" not in fake.names()
    assert any("AWS_SECRET_NAME" in line for line in checks["secrets.names"].detail)


@pytest.mark.parametrize("merge, status", [(None, "warn"), ("false", "warn"), ("true", "ok")])
def test_google_sign_in_without_merge_by_email_warns(hub, monkeypatch, merge, status):
    install(monkeypatch, {"hubsec": {}})
    os.environ["GOOGLE_CLIENT_ID"] = "client-id"
    if merge is not None:
        os.environ["OAUTH_MERGE_ACCOUNTS_BY_EMAIL"] = merge
    check = _by_id(doc.run(hub))["auth.google_merge"]
    assert check.status == status


def test_google_merge_is_read_from_the_deployment_secret(hub, monkeypatch):
    with (hub / ".env").open("a") as f:
        f.write("AWS_SECRET_NAME=dep\n")
    install(monkeypatch, {"hubsec": {}, "dep": {"GOOGLE_CLIENT_ID": "id", "OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "true"}})
    checks = _by_id(doc.run(hub))
    assert checks["secrets.deployment"].status == "ok"
    assert checks["auth.google_merge"].status == "ok"


def test_no_google_client_no_check(hub, monkeypatch):
    install(monkeypatch, {"hubsec": {}})
    assert "auth.google_merge" not in _by_id(doc.run(hub))
