"""Surface-level tests for `hubzoid run` CLI flags.

We don't actually start the bridge or OWUI here. We inspect the command's
option declarations and function signature to confirm flags are exposed and
have the right defaults.
"""
from __future__ import annotations

import inspect

from hubzoid import cli


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
