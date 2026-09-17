"""Gateway CLI -> real subprocess dotenv loading -> CLI/portal database discovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock

from typer.testing import CliRunner

from hubzoid import cli, deployment, webui


def test_gateway_child_configuration_is_isolated_and_discoverable(
    tmp_path, monkeypatch
):
    for key in (
        "HUBZOID_DEPLOYMENT",
        "HUBZOID_OPERATIONAL_DB",
        "HUBZOID_DBOS_DB",
        "DATABASE_URL",
        "OWUI_INTERNAL_URL",
        "HUBZOID_GATEWAY",
        "HUBZOID_DISABLE_EDGE",
        "OWUI_NATIVE_MCP",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "synthetic-admin@example.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "synthetic-test-password")
    monkeypatch.setenv(
        "WEBUI_AUTH", "false"
    )  # no provisioning in this isolated harness
    hubs = [tmp_path / name for name in ("finance", "ops")]
    for index, hub in enumerate(hubs):
        hub.mkdir()
        (hub / "AGENTS.md").write_text(f"---\nname: {hub.name}\n---\nTest agent.")
        (hub / ".env").write_text(
            f"BRIDGE_PORT={18400+index}\n"
            + ("FINANCE_PRIVATE_TOKEN=only-finance\n" if index == 0 else "")
        )
    snapshots = []
    probe = """import json, os, sys
from pathlib import Path
from hubzoid import settings, db, deployment
hub=Path(sys.argv[1]); settings.load(hub)
print(json.dumps(dict(op=db.operational_url(hub), dbos=db.dbos_url(hub), ui=deployment.owui_url(hub),
 private=os.environ.get('FINANCE_PRIVATE_TOKEN'), admin=os.environ.get('HUBZOID_GATEWAY_ADMIN_EMAIL'),
 password_present=bool(os.environ.get('HUBZOID_GATEWAY_ADMIN_PASSWORD')))))
"""
    # Save the original Popen; subprocess.run uses the module's Popen at runtime.
    real_popen = subprocess.Popen

    def spawn(command, env=None, **kwargs):
        if command[:3] == [sys.executable, "-m", "hubzoid"] and "run" in command:
            child = real_popen(
                [sys.executable, "-c", probe, command[4]],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            out, err = child.communicate(timeout=30)
            assert child.returncode == 0, err
            snapshots.append(json.loads(out))
        process = MagicMock()
        process.poll.return_value = None
        return process

    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    monkeypatch.setattr(cli, "_wait_for", lambda *args, **kwargs: True)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    fake_webui = MagicMock()
    fake_webui.wait.return_value = 0
    monkeypatch.setattr(webui, "start_gateway", lambda **kwargs: fake_webui)
    result = CliRunner().invoke(
        cli.app,
        [
            "gateway",
            *[str(h) for h in hubs],
            "--data-dir",
            str(tmp_path / "gateway"),
            "--port",
            "13880",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(snapshots) == 2
    assert snapshots[0]["op"] == snapshots[1]["op"]
    assert snapshots[0]["dbos"] != snapshots[1]["dbos"]
    assert snapshots[0]["private"] == "only-finance"
    assert snapshots[1]["private"] is None
    assert all(
        s["admin"] == "synthetic-admin@example.com" and s["password_present"]
        for s in snapshots
    )
    assert snapshots[0]["ui"] == snapshots[1]["ui"]
    assert deployment.read(hubs[0])["operational_url"] == snapshots[0]["op"]
    assert "FINANCE_PRIVATE_TOKEN" not in os.environ
    # A separate operator process discovers exactly the same store via pointer.
    child = real_popen(
        [sys.executable, "-c", probe, str(hubs[1])],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = child.communicate(timeout=30)
    assert child.returncode == 0, err
    assert json.loads(out)["op"] == snapshots[0]["op"]


def test_duplicate_access_domains_refused_before_gateway_start(tmp_path):
    a, b = tmp_path / "one" / "hub", tmp_path / "two" / "hub"
    for hub in (a, b):
        hub.mkdir(parents=True)
        (hub / "AGENTS.md").write_text("test")
    result = CliRunner().invoke(
        cli.app, ["gateway", str(a), str(b), "--data-dir", str(tmp_path / "gateway")]
    )
    assert result.exit_code == 2
    # Rich/click wraps the CLI message across lines, so normalize whitespace
    # before matching rather than depending on where the line break lands.
    assert "access domains cannot overlap" in " ".join(result.output.split())
    assert not (tmp_path / "gateway" / "deployment.json").exists()
