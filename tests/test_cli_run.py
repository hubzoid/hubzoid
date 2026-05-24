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
