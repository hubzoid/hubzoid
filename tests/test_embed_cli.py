"""hubzoid embed: make a secret, list apps, mint a test pass."""
from __future__ import annotations

import os
import time

from typer.testing import CliRunner

from hubzoid.cli import app as cli_app
from tests.chat_helpers import make_hub


def test_secret_list_and_pass(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch, name="finance")
    runner = CliRunner()
    made = runner.invoke(cli_app, ["embed", "secret", "erpnext", "--hub", str(hub), "--origin",
                                   "https://erp.example.com", "--token-to", "app_lookup", "--token-to",
                                   "connector:erp"])
    assert made.exit_code == 0, made.output
    assert "HUBZOID_CLIENT_ID=erpnext" in made.output and "HUBZOID_URL=" in made.output
    again = runner.invoke(cli_app, ["embed", "secret", "erpnext", "--hub", str(hub)])
    assert again.exit_code == 4 and "--rotate" in again.output
    # The printed entry is pasted as is.
    lines = made.output.splitlines()
    start = lines.index("  erpnext:")
    entry = "\n".join(lines[start:lines.index("", start)])
    (hub / "apps.yaml").write_text("apps:\n" + entry + "\n")
    os.utime(hub / "apps.yaml", ns=(time.time_ns() + 10**9,) * 2)
    from hubzoid.embed import config

    config.reset()
    listed = runner.invoke(cli_app, ["embed", "list", "--hub", str(hub), "--json"])
    assert listed.exit_code == 0, listed.output
    import json

    rows = json.loads(listed.output)["apps"]
    assert rows[0]["id"] == "erpnext" and rows[0]["ready"] is True
    assert rows[0]["origins"] == ["https://erp.example.com"] and rows[0]["token_to"] == ["app_lookup", "connector:erp"]
    rotated = runner.invoke(cli_app, ["embed", "secret", "erpnext", "--hub", str(hub), "--rotate"])
    assert rotated.exit_code == 0 and "old secret stops working" in rotated.output
    minted = runner.invoke(cli_app, ["embed", "pass", "erpnext", "alice", "--hub", str(hub)])
    assert minted.exit_code == 0, minted.output
    from hubzoid.embed import tokens

    caller = tokens.verify(hub, minted.output.strip())
    assert caller.user_id == "alice" and caller.app_id == "erpnext"
