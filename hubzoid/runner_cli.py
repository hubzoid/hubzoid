"""`hubzoid runner serve|mcp|start|hello`: the AgentCore runner's commands.

Outside the ``hubzoid.runner`` package on purpose: the CLI registers these at
start, and nothing of the runner is imported until one of them runs."""
from __future__ import annotations

import json
from pathlib import Path

import typer

runner_app = typer.Typer(
    help="Run agent turns on Amazon Bedrock AgentCore: `serve` and `mcp` run inside the "
         "AgentCore runtime; `hello` checks a hub's runner from the hub service.",
    no_args_is_help=True)


@runner_app.command("serve")
def serve_cmd(
    hub: Path = typer.Argument(..., help="Hub directory (inside the image or on EFS)."),
    port: int = typer.Option(8080, "--port", help="AgentCore's HTTP contract port."),
) -> None:
    """Serve the AgentCore HTTP contract (/invocations, /ping, /warmup) for one hub."""
    from .runner.server import serve

    serve(hub.resolve(), port=port)


@runner_app.command("mcp")
def mcp_cmd(
    hub: Path = typer.Argument(..., help="Hub directory (inside the image or on EFS)."),
    port: int = typer.Option(8000, "--port", help="AgentCore's MCP contract port."),
) -> None:
    """Serve the hub's MCP server as an AgentCore MCP runtime."""
    from .runner.mcp_app import serve

    serve(hub.resolve(), port=port)


@runner_app.command("start")
def start_cmd(
    hub: Path = typer.Argument(None, help="Hub directory. Default: HUBZOID_HUB_DIR."),
) -> None:
    """The image's start command: what to run is chosen by HUBZOID_ROLE, because
    AgentCore passes a container environment variables, never arguments.
    ``runner`` serves turns (8080), ``mcp`` the MCP server (8000), unset the
    hub service (``hubzoid run``). On AgentCore the runner and MCP server run
    under AWS's OpenTelemetry instrumentation when it is installed."""
    import os
    import shutil
    import sys

    from .runner import role

    hub = hub or Path(os.environ.get("HUBZOID_HUB_DIR") or ".")
    hub = str(hub.resolve())
    exe = shutil.which("hubzoid") or sys.argv[0]
    current = role.current()
    if current in (role.RUNNER, role.MCP):
        argv = [exe, "runner", "serve" if current == role.RUNNER else "mcp", hub]
        otel = shutil.which("opentelemetry-instrument")
        if otel and (os.environ.get("HUBZOID_OTEL") or "on").lower() not in ("0", "off", "false"):
            argv = [otel, *argv]
    elif current:
        raise typer.BadParameter(f"{role.ENV} must be runner, mcp or unset (it is {current!r}).")
    else:
        argv = [exe, "run", hub]
    os.execv(argv[0], argv)


@runner_app.command("hello")
def hello_cmd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Print the answer as JSON."),
) -> None:
    """Ask this hub's AgentCore runner for its version, without a model call."""
    from . import settings as settingslib
    from .runner.client import RunnerError, hello

    hub = hub.resolve()
    settings = settingslib.load(hub)
    if settings.runner != "agentcore":
        typer.echo("This hub runs its turns locally (HUBZOID_RUNNER is not agentcore).")
        raise typer.Exit(1)
    try:
        out = hello(hub, settings)
    except RunnerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
    else:
        typer.echo(f"Runner {out.get('runner_version', '?')} (protocol {out.get('protocol', '?')}), "
                   f"hub service {out.get('hub_version')}, answered in {out.get('seconds')} s.")
