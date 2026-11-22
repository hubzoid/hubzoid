"""`hubzoid chat operations submit`: a message sent from the CLI gets a reply
(scripted test runtime). From S0's coverage work (2.1.2): the CLI's part
validator lacked the actor the engine passes, so every send crashed."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


def cli(*args):
    """`hubzoid chat <args> --json`: the exit code and the parsed output."""
    r = CliRunner().invoke(app, ["chat", *args, "--json"], catch_exceptions=False)
    return r.exit_code, json.loads(r.output)


def created(hub):
    code, out = cli("conversations", "create", str(hub))
    assert code == 0, out
    return out["conversation_id"], out["branch"]


def test_cli_send_writes_a_reply(hub, tmp_path):
    cid, branch = created(hub)
    body = tmp_path / "send.json"
    body.write_text(json.dumps({"kind": "send", "branch_id": branch["id"], "expected_revision": branch["revision"],
                                "parent_id": None, "content": [{"type": "text", "text": "hi"}]}))
    code, out = cli("operations", "submit", str(hub), "--conversation", cid, "--file", str(body))
    assert code == 0 and out["run"]["status"] == "complete"
