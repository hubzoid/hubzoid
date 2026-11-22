"""`hubzoid hub settings|configure|doctor` for people (the JSON forms are in
tests/test_hub_settings.py): the settings table and schema, secrets shown by
reference only, and the refusals for a missing file, both modes at once and a
folder that is not a hub."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from hubzoid.cli import app
from tests.test_hub_settings import hub, isolated  # noqa: F401


def run(*args):
    r = CliRunner().invoke(app, ["hub", *args], env={"COLUMNS": "200"}, catch_exceptions=False)
    return r.exit_code, r.output


def test_settings_table_and_schema_for_people(hub):  # noqa: F811
    (hub / "hub.settings.yaml").write_text("version: 1\nvalues:\n  FACTORY_REPO: acme/widgets\n  MAX_RETRIES: 9\n"
                                           "secrets:\n  GITHUB_TOKEN_ref: env:GH_TOKEN_FOR_TEST\n")
    code, text = run("settings", str(hub))
    assert code == 0 and "FACTORY_REPO" in text and '"acme/widgets"' in text
    assert "env:GH_TOKEN_FOR_TEST (not set)" in text and "revision" in text
    assert "MAX_RETRIES" in text and "invalid" in text                     # 9 is above the maximum of 5
    code, text = run("settings", str(hub), "--schema")
    assert code == 0 and "FACTORY_REPO (string, required)" in text and "GITHUB_TOKEN (string, required, secret)" in text
    code, out = run("settings", str(hub), "--schema", "--json")
    assert {d["name"] for d in json.loads(out)["settings"]} >= {"FACTORY_REPO", "GITHUB_TOKEN"}


def test_a_hub_without_declarations_says_so(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "AGENTS.md").write_text("---\nname: bare\n---\nx")
    code, text = run("settings", str(bare))
    assert code == 0 and "declares no settings" in text


def test_configure_refusals(hub, tmp_path):  # noqa: F811
    code, out = run("configure", str(hub), "--check", "--apply", "--json")
    assert code == 2 and json.loads(out)["detail"]["message"] == "Use --check or --apply, not both."
    code, out = run("configure", str(hub), "--json")
    assert code == 2 and "does not exist" in json.loads(out)["detail"]["message"]
    bad = tmp_path / "values.yaml"
    bad.write_text("version: 1\nvalues:\n  FACTORY_REPO: not a repo\n")
    code, text = run("configure", str(hub), "--file", str(bad))
    assert code == 2 and "•" in text
    code, out = run("settings", str(tmp_path), "--json")
    assert code == 2 and "is not a hub folder" in json.loads(out)["detail"]["message"]
