"""`hubzoid hub-changes`: propose, list, show, decide and undo from the CLI.
A decision needs a person at a terminal, so the non-interactive runner is
refused, and the approval itself goes through the service as in
tests/test_hub_changes.py."""
from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from hubzoid.hub_changes import service as hc
from tests.chat_helpers import make_hub

OWNER, ANA, BEN = "owner@example.org", "ana@example.org", "ben@example.org"
RULE = [{"path": "knowledge/pricing.md", "old_text": "Orders over 5,000 need one check.",
         "new_text": "Orders over 5,000 need a second check."}]


def _git(hub, *args):
    return subprocess.run(["git", "-C", str(hub), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    (hub / "knowledge").mkdir(exist_ok=True)
    (hub / "knowledge" / "pricing.md").write_text("# Pricing\n\nOrders over 5,000 need one check.\n")
    from hubzoid import access

    gs = access.store_for(hub)
    gs.grant(OWNER, hub.name, "manage_access", actor="t")
    gs.grant(ANA, hub.name, "curator", actor="t")
    for who in (OWNER, ANA, BEN):
        gs.grant(who, hub.name, "use_hub", actor="t")
    _git(hub, "init", "-q")
    _git(hub, "add", "-A")
    _git(hub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    return hub


def hc_cli(*args, as_json=True):
    r = CliRunner().invoke(app, ["hub-changes", *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json else r.output)


def test_propose_list_show_and_the_terminal_rule(hub, tmp_path):
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps(RULE))
    code, ch = hc_cli("propose", str(hub), "--title", "Second check", "--edits", str(edits),
                      "--reason", "Audit finding", "--user", ANA)
    assert code == 0 and ch["status"] == "pending" and ch["paths"] == ["knowledge/pricing.md"]
    assert "one check" in (hub / "knowledge" / "pricing.md").read_text()  # nothing written yet

    code, out = hc_cli("propose", str(hub), "--title", "x", "--edits", str(edits), "--user", BEN)
    assert code == 3 and out["detail"]["code"]  # no curator grant

    code, listed = hc_cli("list", str(hub))
    assert code == 0 and [c["id"] for c in listed["items"]] == [ch["id"]]
    code, text = hc_cli("list", str(hub), "--status", "applied", as_json=False)
    assert code == 0 and text.strip() == "No changes."
    code, text = hc_cli("list", str(hub), as_json=False)
    assert f"#{ch['id']}" in text and "Second check" in text

    code, shown = hc_cli("show", str(hub), str(ch["id"]))
    assert code == 0 and "+Orders over 5,000 need a second check." in shown["diff"]
    code, text = hc_cli("show", str(hub), str(ch["id"]), as_json=False)
    assert f"Proposed by {ANA} via cli" in text and "Why: Audit finding" in text
    code, out = hc_cli("show", str(hub), "999")
    assert code == 2 and out["detail"]["message"] == "There is no change #999."

    # The test runner is not a terminal: a person must decide.
    code, out = hc_cli("decide", str(hub), str(ch["id"]), "--decision", "approve", "--user", OWNER)
    assert code == 3 and out["detail"]["code"] == "human_action_required"
    code, out = hc_cli("decide", str(hub), "999", "--decision", "approve")
    assert code == 2 and out["detail"]["code"] == "not_found"


def test_undo_proposes_the_reverse_change(hub):
    ch = hc.propose(hub, title="Second check", edits=RULE, actor=ANA, via="chat")
    done = hc.decide(hub, ch["id"], actor=OWNER, decision="approve", expected_revision=ch["revision"])
    assert done["status"] == "applied"
    code, undo = hc_cli("undo", str(hub), str(ch["id"]), "--user", OWNER)
    assert code == 0 and undo["id"] != ch["id"] and undo["status"] == "pending"
    code, shown = hc_cli("show", str(hub), str(undo["id"]))
    assert "-Orders over 5,000 need a second check." in shown["diff"]


def test_a_folder_that_is_not_a_hub_is_refused(tmp_path):
    code, out = hc_cli("list", str(tmp_path))
    assert code == 2 and "is not a hub folder" in out["detail"]["message"]
