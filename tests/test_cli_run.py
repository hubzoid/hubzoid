"""Surface-level tests for `hubzoid run` CLI flags.

We don't actually start the bridge or OWUI here. We inspect the command's
option declarations and function signature to confirm flags are exposed and
have the right defaults.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from hubzoid import cli

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


def test_run_host_defaults_to_loopback():
    sig = inspect.signature(cli.run)
    assert sig.parameters["host"].default.default == "127.0.0.1"


# ---------------------------------------------------------------------------
# Edge router wiring (#1): OWUI moves to a loopback internal port.
# ---------------------------------------------------------------------------
def test_run_propagates_bridge_port_to_bridge_env(monkeypatch):
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    captured: list[dict] = []

    class _FakeProc:
        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return getattr(self, "returncode", None)

        def terminate(self):
            pass

    def fake_popen(cmd, *a, **kw):
        captured.append({"cmd": cmd, "env": kw.get("env", {})})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_wait_any", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)

    cli.run(hubs=[MINIMAL], port=None, bridge_port=8010,
            host="127.0.0.1", no_ui=True, slack=False)

    bridge = next(c for c in captured if "hubzoid.server:build_app" in c["cmd"])
    assert bridge["env"].get("BRIDGE_PORT") == "8010"


def test_check_hub_refuses_a_tool_file_that_does_not_load(tmp_path):
    """`hubzoid reload` checks first. A broken tools_local file only loses its
    own tools at run time, so the check must still name it."""
    import shutil

    from typer.testing import CliRunner

    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub)
    assert CliRunner().invoke(cli.app, ["check-hub", str(hub)]).exit_code == 0
    (hub / "tools_local").mkdir(exist_ok=True)
    (hub / "tools_local" / "broken.py").write_text("def broken(:\n    pass\n")
    out = CliRunner().invoke(cli.app, ["check-hub", str(hub)])
    assert out.exit_code == 1 and "tools_local/broken.py" in out.output
