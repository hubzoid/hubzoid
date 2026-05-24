"""Surface-level tests for `hubzoid run` CLI flags.

We don't actually start the bridge or OWUI here. We use Typer's CliRunner
against --help to confirm the option is exposed, and inspect the function
signature to confirm the default.
"""
from __future__ import annotations

import inspect

from typer.testing import CliRunner

from hubzoid import cli


def test_run_help_lists_host_flag():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output


def test_run_host_defaults_to_loopback():
    sig = inspect.signature(cli.run)
    assert sig.parameters["host"].default.default == "127.0.0.1"
