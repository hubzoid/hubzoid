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


def test_run_exposes_host_flag():
    # Inspect the option declaration directly rather than scraping Rich-rendered
    # --help text: that output is ANSI-styled and terminal-width dependent, so the
    # literal "--host" substring isn't reliably present across Rich/Typer versions.
    opt = inspect.signature(cli.run).parameters["host"].default
    assert "--host" in opt.param_decls


def test_run_host_defaults_to_loopback():
    sig = inspect.signature(cli.run)
    assert sig.parameters["host"].default.default == "127.0.0.1"


# ---------------------------------------------------------------------------
# Edge router wiring (#1): OWUI moves to a loopback internal port.
# ---------------------------------------------------------------------------
def test_owui_internal_port_default_offset(monkeypatch):
    monkeypatch.delenv("HUBZOID_OWUI_PORT", raising=False)
    assert cli._owui_internal_port(3080) == 43080


def test_owui_internal_port_env_override(monkeypatch):
    monkeypatch.setenv("HUBZOID_OWUI_PORT", "9999")
    assert cli._owui_internal_port(3080) == 9999


def test_owui_internal_port_high_port_falls_back(monkeypatch):
    monkeypatch.delenv("HUBZOID_OWUI_PORT", raising=False)
    # ui_port + 40000 would exceed the cap, so fall back near the ui_port.
    assert cli._owui_internal_port(40000) == 40001


# ---------------------------------------------------------------------------
# The bridge subprocess must be told its REAL port via BRIDGE_PORT. Otherwise
# settings.bridge_port (read inside the bridge process) defaults to 8000 even
# when the bridge is bound to another port via --bridge-port — which makes the
# OTel normalize intercept point the claude subprocess at the wrong port (the
# HUBZOID_PUBLIC_URL artifact-link fallback is wrong for the same reason).
# ---------------------------------------------------------------------------
def test_run_propagates_bridge_port_to_bridge_env(monkeypatch):
    monkeypatch.delenv("BRIDGE_PORT", raising=False)
    captured: list[dict] = []

    class _FakeProc:
        def wait(self):
            return 0

        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_popen(cmd, *a, **kw):
        captured.append({"cmd": cmd, "env": kw.get("env", {})})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)

    cli.run(hub=MINIMAL, port=None, bridge_port=8010,
            host="127.0.0.1", no_ui=True, slack=False)

    bridge = next(c for c in captured if "hubzoid.server:build_app" in c["cmd"])
    assert bridge["env"].get("BRIDGE_PORT") == "8010"
