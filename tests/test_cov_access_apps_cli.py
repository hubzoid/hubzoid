"""`hubzoid apps`: validate, apply, read and set the card configuration, list
cards and invoke one from the CLI, with refusals. Uses the synthetic card hub
of tests/test_mcp_apps.py. No model, no network."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from hubzoid.cli import app
from tests.test_mcp_apps import OWNER, hub  # noqa: F401


def apps(*args, as_json=True):
    r = CliRunner().invoke(app, ["apps", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_validate_get_set_and_apply(hub, tmp_path):  # noqa: F811
    code, out = apps("validate", str(hub))
    assert code == 0 and out["valid"] is True and out["present"] is True
    assert {c["id"] for c in out["resolved_resources"]} == {"order-summary", "broken"}
    code, text = apps("validate", str(hub), as_json=False)
    assert "order-summary: table card for order_summary" in text and "Valid." in text

    # The files are the truth: reading records them as the active revision.
    code, out = apps("config", "get", str(hub))
    assert code == 0 and out["revision"] == 1 and out["applied_by"] == "file" and out["enabled"] is True
    code, text = apps("config", "get", str(hub), as_json=False)
    assert "Revision 1" in text and "order-summary: table card for order_summary" in text

    code, out = apps("apply", str(hub), "--expected-revision", "absent")
    assert code == 4 and out["detail"]["code"] == "revision_conflict" and out["detail"]["current_revision"] == 1
    code, out = apps("apply", str(hub), "--expected-revision", "1")
    assert code == 4 and out["detail"]["code"] == "unchanged"

    code, out = apps("config", "set", str(hub), "/builtin_cards", "not json")
    assert code == 2 and "VALUE must be JSON" in out["detail"]["message"]
    code, out = apps("config", "set", str(hub), "/enabled", "false")
    assert code == 0 and out == {"written": "mcp-apps.yaml", "pointer": "/enabled", "value": False}
    assert "enabled: false" in (hub / "mcp-apps.yaml").read_text()
    code, got = apps("config", "get", str(hub))
    assert got["revision"] == 2 and got["enabled"] is False
    code, text = apps("config", "get", str(hub), as_json=False)
    assert "(cards disabled)" in text


def test_validate_names_errors_and_check_fails(hub):  # noqa: F811
    (hub / "mcp-apps.yaml").write_text("version: 1\nbuiltin_cards: [table, plan]\n")
    code, out = apps("validate", str(hub))
    assert code == 0 and out["valid"] is False and out["errors"]
    code, out = apps("validate", str(hub), "--check")
    assert code == 2
    code, text = apps("validate", str(hub), as_json=False)
    assert "Not valid: nothing was applied." in text
    (hub / "mcp-apps.yaml").unlink()
    code, text = apps("validate", str(hub), as_json=False)
    assert "No mcp-apps.yaml: no tool shows a card." in text


def test_list_and_invoke_a_card(hub, tmp_path):  # noqa: F811
    apps("config", "get", str(hub))  # records the files as the active revision
    code, rows = apps("list", str(hub), "--as", OWNER)
    assert code == 0 and rows == []
    code, text = apps("list", str(hub), "--as", OWNER, as_json=False)
    assert "No cards yet." in text

    bad = tmp_path / "args.json"
    bad.write_text("[1]")
    code, out = apps("invoke", str(hub), "order-summary", "--args-file", str(bad), "--idempotency-key", "k0",
                     "--as", OWNER)
    assert code == 2 and "must hold a JSON object" in out["detail"]["message"]
    args = tmp_path / "ok.json"
    args.write_text(json.dumps({"cursor": "5"}))
    code, out = apps("invoke", str(hub), "order-summary", "--args-file", str(args), "--idempotency-key", "k1",
                     "--as", OWNER)
    assert code == 0, out
    assert "SAMPLE-006" in out["text"] and out["instance"]["card_id"] == "order-summary"
    code, again = apps("invoke", str(hub), "order-summary", "--args-file", str(args), "--idempotency-key", "k1",
                       "--as", OWNER)
    assert code == 0 and again["instance"]["id"] == out["instance"]["id"]  # the same key replays
    code, rows = apps("list", str(hub), "--as", OWNER)
    assert [r["card_id"] for r in rows] == ["order-summary"]
