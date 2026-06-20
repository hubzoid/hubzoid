"""hubzoid CLI — typer app.

Commands:
  hubzoid init [PATH]              Scaffold a hub from the bundled template.
  hubzoid run [PATH]               Start FastAPI bridge + edge + Open WebUI for a hub.
  hubzoid gateway [HUBS...]        One shared Open WebUI fronting many hub bridges.
  hubzoid schedule ...             Inspect / manually fire <hub>/schedule/*.md tasks.
  hubzoid slack run [PATH]         Start the Slack adapter (Socket Mode).
  hubzoid slack manifest [PATH]    Print a Slack App Manifest YAML.
  hubzoid slack systemd [PATH]     Print a systemd unit template.
  hubzoid doctor [PATH]            Validate hub config and report issues.
  hubzoid test [PATH]              Send a hello prompt and assert non-empty response.
  hubzoid version                  Print version.

Path defaults to `.` (the current directory) everywhere.
"""
from __future__ import annotations

import importlib.resources as resources
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from . import settings as settingslib

app = typer.Typer(
    name="hubzoid",
    add_completion=False,
    no_args_is_help=True,
    help="An open-source framework for production AI agents.",
)
console = Console()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
@app.command()
def init(
    name: Path = typer.Argument(
        Path("demo-hub"),
        help="Name of the new hub folder. Created under the current directory. Default: demo-hub.",
    ),
    template: str = typer.Option(
        "minimal",
        "--template", "-t",
        help="Which bundled template to use. 'minimal' (default) scaffolds a tiny, "
        "runnable hub with one example of each file type. 'demo' scaffolds the full "
        "guided tour with a Hubzoid Guide agent, four teaching skills, and six "
        "knowledge pages.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files in the hub folder."),
) -> None:
    """Scaffold a new hub. Also drops agents-repo wrapper files if the parent looks fresh.

    First run in an empty directory:
      $ hubzoid init devops-agent
      → writes ./requirements.txt, ./.gitignore, ./README.md, ./devops-agent/...

    Second run in the same directory:
      $ hubzoid init irs-agent
      → writes ./irs-agent/... only. Parent files are left alone.

    Get the full guided tour instead:
      $ hubzoid init my-hub --template demo

    The result is a Samarth-style multi-hub agents repo built one hub at a time.
    """
    # Resolve. If `name` is just a folder name, drop it under cwd. If it is
    # `.`, init in cwd itself (legacy / "I am already in my hub dir" case).
    if str(name) == ".":
        hub_dir = Path.cwd().resolve()
        is_in_place = True
    else:
        hub_dir = (Path.cwd() / name).resolve() if not name.is_absolute() else name.resolve()
        is_in_place = False

    template_root = _template_root(template)
    if template_root is None:
        available = ", ".join(_available_templates())
        console.print(
            f"[red]Template '{template}' not found.[/red] "
            f"Available: {available or '(none)'}."
        )
        raise typer.Exit(2)

    parent = hub_dir.parent
    # Check parent freshness BEFORE creating the hub folder, so the hub we are
    # about to create does not itself disqualify the parent.
    parent_is_fresh = (not is_in_place) and _parent_looks_fresh(parent, ignore=hub_dir.name)

    hub_dir.mkdir(parents=True, exist_ok=True)

    # 1. Scaffold the hub folder from the bundled template.
    written: list[Path] = []
    skipped: list[Path] = []
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        dst = hub_dir / rel
        if dst.exists() and not force:
            skipped.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written.append(dst)

    # 2. Write .env from a Python constant (not part of the template tree
    # because .env is gitignored). Same skip rules as template files.
    env_dst = hub_dir / ".env"
    if env_dst.exists() and not force:
        skipped.append(env_dst)
    else:
        env_dst.write_text(_STARTER_ENV)
        written.append(env_dst)

    # 3. If the parent looks fresh and we are scaffolding a sub-folder, drop
    # the agents-repo wrapper files. Never overwrite existing ones, with or
    # without --force (parent files are not the hub's concern).
    parent_written: list[Path] = []
    if parent_is_fresh:
        version_str = _installed_version()
        wrapper = _wrapper_files(parent, hub_dir.name, version_str)
        for dst, content in wrapper.items():
            if dst.exists():
                continue
            dst.write_text(content)
            parent_written.append(dst)

    # 3. Report.
    console.print(f"[green]Initialized hub at[/green] {hub_dir}")
    if written:
        console.print(f"  wrote {len(written)} hub files")
    if skipped:
        console.print(f"  skipped {len(skipped)} existing files (use --force to overwrite)")
    if parent_written:
        console.print(f"\n[green]Bootstrapped agents-repo wrapper at[/green] {parent}")
        for p in parent_written:
            console.print(f"  + {p.name}")

    console.print("\nNext:")
    console.print(f"  1. edit {hub_dir.name}/.env if you do not have `claude` CLI logged in")
    console.print(f"  2. hubzoid run {hub_dir.name}")
    if template == "minimal":
        console.print(
            "\n[dim]Want the guided tour instead? "
            f"hubzoid init {hub_dir.name} --template demo --force[/dim]"
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command()
def run(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    port: int = typer.Option(None, "--port", help="Open WebUI port. Default: 3080 (or PORT env)."),
    bridge_port: int = typer.Option(None, "--bridge-port", help="FastAPI bridge port. Default: 8000 (or BRIDGE_PORT env)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface Open WebUI binds to. Use 0.0.0.0 to expose on LAN."),
    no_ui: bool = typer.Option(False, "--no-ui", help="Skip Open WebUI; bridge only."),
    slack: bool = typer.Option(
        False,
        "--slack", "-s",
        help="Also start the Slack adapter (Socket Mode). Reads SLACK_BOT_TOKEN and "
        "SLACK_APP_TOKEN from .env. Soft-fails with a warning if either is missing.",
    ),
) -> None:
    """Start the bridge (+ Open WebUI) for a hub."""
    hub = hub.resolve()
    if not hub.is_dir():
        console.print(f"[red]Hub directory not found:[/red] {hub}")
        # If the user is currently inside a hub folder, point that out.
        cwd = Path.cwd()
        if (cwd / "AGENTS.md").is_file():
            console.print(
                f"[yellow]Your current directory ({cwd}) looks like a hub. "
                f"Try:[/yellow]\n  python -m hubzoid run .   (or just: hubzoid run .)"
            )
        else:
            console.print(
                "[yellow]Tip:[/yellow] paths are resolved against the current "
                f"directory ({cwd}). Run from the repo root, or pass `.` from inside the hub folder."
            )
        raise typer.Exit(2)
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]No AGENTS.md in {hub}. Run `hubzoid init` first.[/red]")
        raise typer.Exit(2)

    settings = settingslib.load(hub)
    ui_port = port or settings.ui_port
    br_port = bridge_port or settings.bridge_port

    # 1. Start the bridge in a subprocess. We pass HUBZOID_HUB_DIR via env so
    #    `hubzoid.server.build_app` knows what to load.
    bridge_env = os.environ.copy()
    bridge_env["HUBZOID_HUB_DIR"] = str(hub)
    bridge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.server:build_app", "--factory",
        "--host", "127.0.0.1", "--port", str(br_port),
        "--log-level", settings.log_level,
    ]
    console.print(f"[cyan]→ bridge[/cyan]  http://127.0.0.1:{br_port}  (hub: {hub.name})")
    bridge_proc = subprocess.Popen(bridge_cmd, env=bridge_env)

    # 2. Wait for the bridge to come up before starting Open WebUI.
    if not _wait_for(f"http://127.0.0.1:{br_port}/healthz", timeout=60):
        console.print("[red]bridge failed to come up[/red]")
        bridge_proc.terminate()
        raise typer.Exit(1)
    console.print("[green]→ bridge[/green]  ready")

    ui_proc = None
    edge_proc = None
    if not no_ui:
        try:
            from . import branding, webui
            from .loaders import agents as agents_loader

            # Apply per-hub branding into every OWUI static dir
            # (frontend/ and static/, see branding.static_dirs). No-op
            # when <hub>/branding/ is absent or empty.
            for sd in branding.static_dirs():
                branding.apply(hub, sd)

            # Pull suggestions from the main agent's frontmatter so the
            # empty-chat screen has quick-start buttons.
            try:
                main_agent = agents_loader.load_main(hub)
                suggestions = list(main_agent.spec.suggestions)
                main_name = main_agent.spec.name
            except Exception:
                suggestions = []
                main_name = _read_main_agent_name(hub)

            # Display-name cascade: the agent's name: from AGENTS.md wins,
            # then the operator's WEBUI_NAME, then "Hubzoid" (final fallback
            # so it never reads as bare "Open WebUI" to a customer). Anchoring
            # on the agent name keeps the login page, the sidebar, and the
            # chat-center model label all showing the same hub name.
            resolved_webui_name = (
                main_name
                or settings.webui_name
                or "Hubzoid"
            )

            # The edge router (hubzoid/edge.py) binds the PUBLIC port and
            # routes /artifacts -> bridge, everything else -> Open WebUI, so
            # artifact download links work behind a single exposed port (the
            # report-download fix; the bridge port need not be exposed). When
            # the edge is on, OWUI moves to a loopback internal port and the
            # edge takes the public bind. Opt out with HUBZOID_DISABLE_EDGE=1.
            edge_enabled = os.environ.get("HUBZOID_DISABLE_EDGE", "").lower() not in ("1", "true", "yes")
            owui_port = _owui_internal_port(ui_port) if edge_enabled else ui_port
            owui_host = "127.0.0.1" if edge_enabled else host

            ui_proc = webui.start(
                hub_dir=hub,
                bridge_port=br_port,
                ui_port=owui_port,
                ui_host=owui_host,
                api_key=settings.first_api_key,
                model_label=settings.model_label or main_name,
                webui_name=resolved_webui_name,
                suggestions=suggestions,
            )
            log_path = getattr(ui_proc, "_log_path", None)
            console.print(f"[cyan]→ webui [/cyan]  starting (Open WebUI; local embedding model is off, so boot is quick)")
            if log_path:
                console.print(f"            log: {log_path}")

            # Wait for OWUI on its (now possibly internal) bind before fronting it.
            owui_probe = "127.0.0.1" if owui_host in ("0.0.0.0", "::") else owui_host
            owui_ready = _wait_for(f"http://{owui_probe}:{owui_port}/", timeout=240)

            probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            display_url = f"http://{host}:{ui_port}"
            if edge_enabled:
                # Start the public-facing edge router in front of bridge + OWUI.
                edge_env = os.environ.copy()
                edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{owui_port}"
                edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps([
                    {"prefix": "/artifacts", "upstream": f"http://127.0.0.1:{br_port}"}
                ])
                edge_cmd = [
                    sys.executable, "-m", "uvicorn",
                    "hubzoid.edge:_factory", "--factory",
                    "--host", host, "--port", str(ui_port),
                    "--log-level", settings.log_level,
                ]
                console.print(f"[cyan]→ edge  [/cyan]  http://{host}:{ui_port}  (/artifacts → bridge :{br_port}, else → owui :{owui_port})")
                edge_proc = subprocess.Popen(edge_cmd, env=edge_env)
                edge_ready = _wait_for(f"http://{probe_host}:{ui_port}/", timeout=30)
                if owui_ready and edge_ready and edge_proc.poll() is None:
                    console.print(f"[green]→ webui [/green]  ready    {display_url}")
                else:
                    console.print(f"[yellow]→ webui [/yellow]  did not become ready in time; check log above. URL: {display_url}")
            else:
                if owui_ready:
                    console.print(f"[green]→ webui [/green]  ready    {display_url}")
                else:
                    console.print(f"[yellow]→ webui [/yellow]  did not become ready in 4 min; check log above. URL: {display_url}")
        except FileNotFoundError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            console.print("Bridge only. Curl http://127.0.0.1:" + str(br_port) + "/v1/chat/completions to chat.")

    # Optional: spawn the Slack adapter as a third child. Soft-warn if the
    # operator asked for --slack but the .env is missing the tokens — the
    # bridge + UI keep running either way.
    slack_proc = None
    if slack:
        from .slack.env import should_start_slack
        ok, warn = should_start_slack(want_slack=True, env=os.environ)
        if not ok:
            console.print(f"[yellow]→ slack [/yellow]  skipping: {warn}")
        else:
            slack_cmd = [sys.executable, "-m", "hubzoid", "slack", "run", str(hub)]
            slack_proc = subprocess.Popen(slack_cmd, env=bridge_env)
            console.print(f"[cyan]→ slack [/cyan]  starting (Socket Mode)")

    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down...[/cyan]")
        for p in (edge_proc, ui_proc, slack_proc, bridge_proc):
            if p is not None and p.poll() is None:
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # Block on the bridge process; its exit ends the CLI.
    try:
        bridge_proc.wait()
    finally:
        for p in (edge_proc, ui_proc, slack_proc):
            if p is not None and p.poll() is None:
                p.terminate()


# ---------------------------------------------------------------------------
# gateway — one Open WebUI fronting many hub bridges
# ---------------------------------------------------------------------------
@app.command()
def gateway(
    hubs: list[Path] = typer.Argument(..., help="Hub directories to front with one shared Open WebUI."),
    port: int = typer.Option(None, "--port", help="Public port the shared UI is reached on. Default: 3080 (or PORT env)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface the public edge binds to. Use 0.0.0.0 to expose."),
    public_url: str = typer.Option(None, "--public-url", help="Public base URL (e.g. https://hub.example.com); used to build per-hub artifact download links. Falls back to HUBZOID_PUBLIC_URL."),
    name: str = typer.Option("Hubzoid", "--name", help="Shared Open WebUI display name."),
    data_dir: Path = typer.Option(None, "--data-dir", help="Shared Open WebUI state dir. Default: ./.hubzoid-gateway."),
    launch_bridges: bool = typer.Option(True, "--launch-bridges/--no-bridges", help="Launch each hub's headless bridge. --no-bridges fronts bridges already running as separate units."),
) -> None:
    """Run ONE Open WebUI over many hubs — one headless bridge per hub.

    Lighter than one `hubzoid run` per hub (a single OWUI process instead of
    N). Each hub surfaces as a selectable model; gate per-team access with
    OWUI Groups + per-model Private ACL. Artifact downloads route per hub via
    `/b/<slug>/artifacts`. See docs/DEPLOYING.md.
    """
    from . import gateway as gateway_lib
    from . import webui

    hub_dirs = [h.resolve() for h in hubs]
    for h in hub_dirs:
        if not (h / "AGENTS.md").is_file():
            console.print(f"[red]Not a hub (no AGENTS.md):[/red] {h}")
            raise typer.Exit(2)

    try:
        gp = gateway_lib.plan(hub_dirs)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    ui_port = port or int(os.environ.get("PORT", "3080"))
    pub = (public_url or os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    gw_data = (data_dir or (Path.cwd() / ".hubzoid-gateway")).resolve()
    log_level = os.environ.get("HUB_LOG_LEVEL", "info")

    procs: list[subprocess.Popen] = []

    # 1. Launch each hub's headless bridge (unless they already run elsewhere).
    if launch_bridges:
        for b in gp.backends:
            bridge_env = os.environ.copy()
            # Per-hub public base so this bridge's artifact links resolve
            # through the edge back to itself. Only injected when the hub's
            # own .env doesn't already pin HUBZOID_PUBLIC_URL.
            if pub:
                bridge_env["HUBZOID_PUBLIC_URL"] = gp.public_url_for(pub, b)
            cmd = [
                sys.executable, "-m", "hubzoid", "run", str(b.hub_dir),
                "--no-ui", "--bridge-port", str(b.bridge_port),
            ]
            procs.append(subprocess.Popen(cmd, env=bridge_env))
            console.print(f"[cyan]→ bridge[/cyan]  {b.slug}  http://127.0.0.1:{b.bridge_port}")

    # 2. Wait for every bridge to be healthy.
    for b in gp.backends:
        if not _wait_for(f"http://127.0.0.1:{b.bridge_port}/healthz", timeout=60):
            console.print(f"[red]bridge {b.slug} (:{b.bridge_port}) failed to come up[/red]")
            for p in procs:
                p.terminate()
            raise typer.Exit(1)
    console.print(f"[green]→ bridges[/green]  {len(gp.backends)} ready: {', '.join(b.slug for b in gp.backends)}")

    # 3. One shared Open WebUI, on a loopback internal port behind the edge.
    edge_enabled = os.environ.get("HUBZOID_DISABLE_EDGE", "").lower() not in ("1", "true", "yes")
    owui_port = _owui_internal_port(ui_port) if edge_enabled else ui_port
    owui_host = "127.0.0.1" if edge_enabled else host
    try:
        owui_proc = webui.start_gateway(
            data_dir=gw_data,
            ui_port=owui_port,
            ui_host=owui_host,
            connection_env=gp.connection_env(),
            webui_name=name,
        )
    except FileNotFoundError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        for p in procs:
            p.terminate()
        raise typer.Exit(1)
    procs.append(owui_proc)
    log_path = getattr(owui_proc, "_log_path", None)
    console.print(f"[cyan]→ webui [/cyan]  shared, fronting {len(gp.backends)} hubs (first run downloads nothing — embedding model is off)")
    if log_path:
        console.print(f"            log: {log_path}")
    owui_probe = "127.0.0.1" if owui_host in ("0.0.0.0", "::") else owui_host
    owui_ready = _wait_for(f"http://{owui_probe}:{owui_port}/", timeout=240)

    # 4. The public edge: per-hub artifact prefixes -> bridges, rest -> OWUI.
    edge_proc = None
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    display_url = f"http://{host}:{ui_port}"
    if edge_enabled:
        edge_env = os.environ.copy()
        edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{owui_port}"
        edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(gp.edge_routes())
        edge_cmd = [
            sys.executable, "-m", "uvicorn",
            "hubzoid.edge:_factory", "--factory",
            "--host", host, "--port", str(ui_port),
            "--log-level", log_level,
        ]
        edge_proc = subprocess.Popen(edge_cmd, env=edge_env)
        procs.append(edge_proc)
        edge_ready = _wait_for(f"http://{probe_host}:{ui_port}/", timeout=30)
        if owui_ready and edge_ready:
            console.print(f"[green]→ gateway[/green]  ready    {display_url}")
        else:
            console.print(f"[yellow]→ gateway[/yellow]  not fully ready; check logs. URL: {display_url}")
    else:
        console.print(f"[green]→ gateway[/green]  ready    {display_url}" if owui_ready else f"[yellow]→ gateway[/yellow]  OWUI not ready; check logs.")

    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down gateway...[/cyan]")
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # Block on the shared OWUI; its exit ends the gateway.
    try:
        owui_proc.wait()
    finally:
        for p in procs:
            if p is not owui_proc and p.poll() is None:
                p.terminate()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
@app.command()
def doctor(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Validate a hub: AGENTS.md, sub-agents, skills, knowledge, tools, .env."""
    hub = hub.resolve()
    problems: list[str] = []
    notes: list[str] = []

    if not hub.is_dir():
        console.print(f"[red]Hub directory not found:[/red] {hub}")
        raise typer.Exit(2)

    if not (hub / "AGENTS.md").is_file():
        problems.append("missing AGENTS.md at hub root")

    env_path = hub / ".env"
    if not env_path.is_file():
        notes.append(f"no .env at {env_path} (run `hubzoid init` to scaffold one, or create it by hand)")

    # Try to actually build the runtime — this is the most thorough check.
    # Picks the backend based on MODEL (openai-agents by default,
    # claude-local when MODEL=claude-local).
    try:
        from . import runtime as runtime_lib
        rt = runtime_lib.build(hub)
        notes.append(f"runtime built: {rt.name!r} via {type(rt).__name__}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"runtime build failed: {type(exc).__name__}: {exc}")

    # Scheduled tasks: parse + cron-validate <hub>/schedule/*.md.
    try:
        from . import scheduling as sch
        stasks, sproblems = sch.load_tasks(hub)
        enabled = [t for t in stasks if t.enabled]
        if stasks:
            extra = f", {len(stasks) - len(enabled)} disabled" if len(stasks) != len(enabled) else ""
            notes.append(f"schedule: {len(enabled)} enabled task(s){extra}")
        problems.extend(f"schedule/{p}" for p in sproblems)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"schedule load failed: {type(exc).__name__}: {exc}")

    for n in notes:
        console.print(f"[green]✓[/green] {n}")
    for p in problems:
        console.print(f"[red]✗[/red] {p}")
    if problems:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------
@app.command("test")
def test_hub(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    prompt: str = typer.Option("Reply with the single word: pong", "--prompt", help="Test prompt to send."),
) -> None:
    """Send one prompt to the hub's agent and print the response.

    Runs in-process (no bridge / no UI). Backend is picked from MODEL in .env:
    `claude-local` -> Claude Agent SDK; anything else -> OpenAI Agents SDK.
    """
    import asyncio

    hub = hub.resolve()

    from . import runtime as runtime_lib

    # No MODEL in .env is fine — runtime.build() defaults to claude-local
    # (Claude Agent SDK on Sonnet via the bundled `claude` login).
    rt = runtime_lib.build(hub)
    console.print(f"[cyan]→[/cyan] {prompt}")

    async def _go() -> str:
        # Open/use/close MCP in one task — see runtime.aopen() for why.
        await rt.aopen()
        try:
            return await rt.run(prompt)
        finally:
            await rt.aclose()

    text = asyncio.run(_go())
    console.print(f"[green]←[/green] {text}")


# ---------------------------------------------------------------------------
# slack
# ---------------------------------------------------------------------------
slack_app = typer.Typer(
    help="Slack chat surface: run the adapter or generate config artifacts.",
    no_args_is_help=True,
)


@slack_app.command("run")
def slack_run(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Start the Slack adapter (Socket Mode). Foreground; ^C to stop.

    Requires the hub's bridge to be running separately (`hubzoid run <hub>`).
    Reads SLACK_BOT_TOKEN and SLACK_APP_TOKEN from <hub>/.env.
    """
    from . import slack as slack_pkg
    from .slack.adapter import run as run_adapter
    from .slack.env import EnvError

    hub = hub.resolve()
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]No AGENTS.md in {hub}. Run `hubzoid init` first.[/red]")
        raise typer.Exit(2)

    # Trigger .env load so SLACK_* vars are visible to the adapter and
    # settings.load() sees the same picture as `hubzoid run`.
    settingslib.load(hub)

    try:
        rc = run_adapter(hub)
        raise typer.Exit(rc)
    except EnvError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@slack_app.command("manifest")
def slack_manifest(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    format: str = typer.Option(
        "json",
        "--format", "-f",
        help="Output format: json (default, terminal-friendly) or yaml.",
        case_sensitive=False,
    ),
) -> None:
    """Print a Slack App Manifest pre-filled from <hub>/AGENTS.md.

    Paste the output into https://api.slack.com/apps -> "Create New App"
    -> "From a manifest" to scaffold the bot. Then copy SLACK_BOT_TOKEN and
    SLACK_APP_TOKEN into <hub>/.env and run `hubzoid slack run <hub>`.
    """
    from .slack.manifest import manifest_for_hub

    hub = hub.resolve()
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]No AGENTS.md in {hub}.[/red]")
        raise typer.Exit(2)
    fmt = format.lower()
    if fmt not in ("json", "yaml"):
        console.print(f"[red]--format must be json or yaml, got {format!r}[/red]")
        raise typer.Exit(2)
    # Print to stdout (not console) so the output round-trips through `> file.json`.
    typer.echo(manifest_for_hub(hub, format=fmt))


@slack_app.command("systemd")
def slack_systemd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    user: str = typer.Option("hubzoid", "--user", help="Linux user to run the service as."),
    python: Path = typer.Option(
        None, "--python", help="Python interpreter path. Default: detect from current sys.executable."
    ),
) -> None:
    """Print a systemd unit for hubzoid-slack@<hub>.service to stdout."""
    from .slack.service import systemd_unit_for_hub

    hub = hub.resolve()
    python_path = python or Path(sys.executable).resolve()
    typer.echo(systemd_unit_for_hub(hub_dir=hub, python_path=python_path, user=user))


app.add_typer(
    slack_app,
    name="slack",
    help="Slack chat surface: run the adapter or generate config artifacts.",
    rich_help_panel="Commands",
)


# ---------------------------------------------------------------------------
# schedule — hub-owned background tasks under <hub>/schedule/*.md
# ---------------------------------------------------------------------------
schedule_app = typer.Typer(
    help="Hub-owned scheduled tasks: one md file per job under <hub>/schedule/. "
    "They fire automatically inside `hubzoid run`; these commands inspect and "
    "manually fire them.",
    no_args_is_help=True,
)


@schedule_app.command("list")
def schedule_list(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """List the hub's scheduled tasks, their cadence and next fire time."""
    from datetime import datetime

    from . import scheduling as sch

    hub = hub.resolve()
    tasks, problems = sch.load_tasks(hub)
    state = sch.ScheduleState(hub)
    now = datetime.now()
    if not tasks and not problems:
        console.print(f"no tasks — add markdown files under {hub / 'schedule'}/")
        return
    for t in tasks:
        nxt = sch.next_fire_for(t, state, now)
        entry = state.get(t.name)
        last = entry.get("last_fired_iso")
        last_s = f"last: {entry.get('last_result', '?')} @ {last}" if last else "never fired"
        flags = []
        if t.commit:
            flags.append(f"commit: {', '.join(t.commit)}" + (" + push" if t.push else ""))
        if not t.enabled:
            console.print(f"[dim]⏸ {t.name}  ({t.schedule})  disabled[/dim]")
            continue
        console.print(
            f"[green]●[/green] [bold]{t.name}[/bold]  {t.schedule} ({sch.cron_to_human(t.cron)})"
            f"  →  next {nxt.strftime('%Y-%m-%d %H:%M') if nxt else 'never'}  ·  {last_s}"
            + (f"  ·  {'; '.join(flags)}" if flags else "")
        )
    for p in problems:
        console.print(f"[red]✗ {p}[/red]")
    if problems:
        raise typer.Exit(1)


@schedule_app.command("run")
def schedule_run(
    hub: Path = typer.Argument(..., help="Hub directory."),
    task_name: str = typer.Argument(..., metavar="TASK", help="Task name (the md filename stem)."),
    timeout: int = typer.Option(None, "--timeout", help="Override the task's per-round timeout (seconds)."),
    max_rounds: int = typer.Option(None, "--max-rounds", help="Override the task's round cap."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the round-1 prompt and exit (no LLM)."),
) -> None:
    """Fire one task NOW, in-process — for testing and manual runs.

    Uses the hub's configured MODEL (claude-local or any OpenAI/LiteLLM id),
    the same as a scheduler fire. Ignores the cron schedule and the idle
    gate, but still takes the run lock, so it can't overlap a scheduler run.
    Exit 0 = the agent reported DONE; 1 = incomplete/error.
    """
    import asyncio
    import logging as _logging

    from . import schedule_runner as runner
    from . import scheduling as sch

    hub = hub.resolve()
    tasks, problems = sch.load_tasks(hub)
    by_name = {t.name: t for t in tasks}
    if task_name not in by_name:
        known = ", ".join(sorted(by_name)) or "(none)"
        console.print(f"[red]no task {task_name!r} under {hub / 'schedule'}/. Known: {known}[/red]")
        for p in problems:
            console.print(f"[red]✗ {p}[/red]")
        raise typer.Exit(2)
    task = by_name[task_name]
    if timeout:
        task.timeout = timeout
    if max_rounds:
        task.max_rounds = max_rounds

    if dry_run:
        typer.echo(runner.build_prompt(task, hub, round_no=1))
        return

    # Manual runs should be observable in the terminal.
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lock = sch.RunLock(hub)
    if not lock.acquire(task.name):
        console.print("[red]another scheduled run is in progress (lock held); try again later.[/red]")
        raise typer.Exit(1)
    try:
        console.print(f"[cyan]→ running {task.name}[/cyan] (timeout {task.timeout}s/round, ≤{task.max_rounds} rounds)")
        result = asyncio.run(runner.run_task(hub, task))
    finally:
        lock.release()

    console.print(f"[dim]run log: {result.run_log}[/dim]")
    if result.ok:
        sha = f" · committed {result.commit_sha[:10]}" if result.commit_sha else ""
        console.print(f"[green]✓ done in {result.rounds} round(s)[/green]: {result.summary or '(no summary)'}{sha}")
    else:
        console.print(f"[red]✗ {result.result} after {result.rounds} round(s)[/red] {result.error}")
        raise typer.Exit(1)


@schedule_app.command("status")
def schedule_status(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Show each task's recorded fire history (anchors, last result, last log)."""
    from . import scheduling as sch

    hub = hub.resolve()
    tasks, _ = sch.load_tasks(hub)
    state = sch.ScheduleState(hub)
    if not tasks:
        console.print(f"no tasks — add markdown files under {hub / 'schedule'}/")
        return
    for t in tasks:
        entry = state.get(t.name)
        console.print(f"[bold]{t.name}[/bold]" + ("" if t.enabled else " [dim](disabled)[/dim]"))
        if not entry:
            console.print("  never seen by a scheduler yet")
            continue
        for key in ("first_seen_iso", "last_fired_iso", "last_result", "last_run_log"):
            if entry.get(key):
                console.print(f"  {key.removesuffix('_iso')}: {entry[key]}")


app.add_typer(
    schedule_app,
    name="schedule",
    help="Inspect / manually fire the hub's scheduled background tasks.",
    rich_help_panel="Commands",
)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Print the installed hubzoid version."""
    console.print(__version__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_STARTER_ENV = """\
# demo-hub configuration. This file is git-ignored.
#
# The default below uses your installed `claude` CLI and Pro/Max subscription
# for inference. No API key needed. Requires `claude login` already done.
#
# To use a hosted provider instead, comment out MODEL=claude-local and
# uncomment one of the alternative stanzas. Set the matching API key.

MODEL=claude-local              # defaults to Sonnet 4.x (decisive on routing rules)
# MODEL=claude-local/sonnet     # explicit; same as bare `claude-local`
# MODEL=claude-local/opus       # opt in to Opus
# MODEL=claude-local/haiku      # opt in to Haiku (~3x faster TTFT, but tends to ask before executing documented workflows)

# Headless / server (no interactive `claude login` on the box): paste a
# subscription token minted with `claude setup-token`. It is NOT an API key
# and is NOT billed per-token — usage draws on your Pro/Max subscription.
# The `claude` CLI reads it automatically. See docs/DEPLOYING.md §5b.
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

# --- OpenRouter (one key, many models) -------------------------------------
# OPENROUTER_API_KEY=
# MODEL=openrouter/anthropic/claude-haiku-4.5
# Tip: at https://openrouter.ai/settings/preferences pin Anthropic first
# (allow fallbacks). Otherwise OpenRouter splits calls across Anthropic /
# Vertex / Bedrock and prompt cache hits get fragmented.

# --- OpenAI -----------------------------------------------------------------
# OPENAI_API_KEY=
# MODEL=openai/gpt-4o-mini

# --- Anthropic --------------------------------------------------------------
# ANTHROPIC_API_KEY=
# MODEL=anthropic/claude-haiku-4-5

# --- Branding / UI ---------------------------------------------------------
# WEBUI_NAME=                   # Login page, sidebar, and chat title. Leave
                                # blank to use the agent's `name:` from
                                # AGENTS.md, so all three read the same hub
                                # name. Set this only to override with a
                                # different display brand. Final fallback when
                                # neither is set: "Hubzoid".
# Logo, favicon, splash: drop files into ./branding/. See ./branding/README.md.
# RESPONSE_WATERMARK=           # watermark on copied messages; defaults to hub name
# DEFAULT_PROMPT_SUGGESTIONS:   # set the `suggestions:` field in AGENTS.md frontmatter
# HUBZOID_KEEP_OWUI_SUFFIX=True # set if your deployment exceeds 50 users in 30 days
                                # (Open WebUI license requires the "(Open WebUI)" suffix
                                # to remain visible above that threshold)

# --- Bridge / UI knobs (all optional) --------------------------------------
# BRIDGE_API_KEYS=dev           # comma-separated; first one is what Open WebUI sees
# MODEL_LABEL=                  # what /v1/models reports; blank = derived from AGENTS.md name
# PORT=3080                     # Open WebUI port
# BRIDGE_PORT=8000              # FastAPI bridge port
# HUBZOID_PUBLIC_URL=           # public base URL for the bridge — used to build
                                # download links emitted by write_artifact. Set
                                # this when behind a reverse proxy or on a
                                # different host than the user's browser.
                                # Default: http://127.0.0.1:<BRIDGE_PORT>
                                # Example: https://hub.example.com
# HTTP_ALLOWLIST=               # comma-separated hostnames the http_get tool may visit
# HUBZOID_DISABLE_HTTP_GET=true # remove http_get from the tool registry entirely
# HUBZOID_DISABLE_WEB_SEARCH=true  # remove web_search from the tool registry entirely

# --- Auth (default: off, single user) --------------------------------------
# Uncomment ONE block below to require login. Full walkthrough + Google /
# Microsoft / GitHub / OIDC / LDAP details: docs/auth.md.

# Mode B: email + password, admin invites users.
# WEBUI_AUTH=true
# ENABLE_SIGNUP=false
# DEFAULT_USER_ROLE=user
# WEBUI_SECRET_KEY=               # openssl rand -hex 32
# WEBUI_URL=https://your.host     # required behind a reverse proxy
# WEBUI_ADMIN_EMAIL=you@you.com   # one-shot: seeds first admin on a fresh DB
# WEBUI_ADMIN_PASSWORD=           # one-shot: delete both ADMIN lines after first boot

# Mode C: Google SSO (use alongside Mode B's lines above).
# ENABLE_OAUTH_SIGNUP=true
# DEFAULT_USER_ROLE=pending       # new Google users wait for admin approval
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
# Authorized redirect URI in Google Console: <WEBUI_URL>/oauth/google/callback

# Opt-in: let users curate per-user memories in OWUI's UI; OWUI injects the
# top matches into the agent's system prompt on every chat. Off by default
# because OWUI flags this feature as Beta and storage format may change.
# ENABLE_MEMORY=true

# --- Slack chat surface (optional, opt-in per agent) ----------------------
# Run `hubzoid slack manifest .` to generate an App Manifest you can paste
# into https://api.slack.com/apps. After installing the app to your workspace
# copy the two tokens here, then run `hubzoid slack run .` next to your hub.
# Full walkthrough: docs/slack.md.
# SLACK_BOT_TOKEN=xoxb-...        # Bot User OAuth Token
# SLACK_APP_TOKEN=xapp-...        # App-Level Token, scope connections:write

# --- Strip flags (advanced) -----------------------------------------------
# Hubzoid sets ~24 Open WebUI flags by default to strip platform surfaces
# (code interpreter, community sharing, etc.). To override any, just add the
# line here. See https://github.com/hubzoid/hubzoid/blob/main/docs/branding.md
# for the full list and what each does.
"""


def _installed_version() -> str:
    """Return the installed hubzoid version, or the source-tree version as a fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _ver
        try:
            return _ver("hubzoid")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    return __version__


def _parent_looks_fresh(parent: Path, *, ignore: str) -> bool:
    """Heuristic: parent is empty enough to be a fresh agents-repo wrapper.

    Empty parent → fresh. Parent that contains only dotfiles, a README, a
    requirements.txt, a LICENSE, a `.venv`, or the hub folder we are about
    to create → also fresh. Anything else (sibling hub folders, src/, etc.)
    means this is an existing project; do not write parent files.
    """
    if not parent.exists():
        return True
    allowed = {"README.md", "requirements.txt", "LICENSE", "LICENSE.md", ".env"}
    for entry in parent.iterdir():
        if entry.name == ignore:
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in allowed:
            continue
        return False
    return True


def _wrapper_files(parent: Path, hub_name: str, version_str: str) -> dict[Path, str]:
    """The agents-repo wrapper files to drop at the parent level on first init."""
    requirements_txt = (
        "# Hubzoid agents repo. One hub per sibling folder.\n"
        "# Replace the pin below with your version. For private mirrors, swap to:\n"
        "#   git+ssh://git@github.com/<org>/<your-mirror>@v<version>#egg=hubzoid\n"
        f"hubzoid=={version_str}\n"
    )
    gitignore = (
        "# Hubzoid\n"
        ".env\n"
        "output/\n"
        ".openwebui-data/\n"
        "\n"
        "# Python\n"
        "__pycache__/\n"
        "*.pyc\n"
        ".venv/\n"
        ".pytest_cache/\n"
        "\n"
        "# OS\n"
        ".DS_Store\n"
    )
    readme = (
        f"# {parent.name}\n"
        "\n"
        "Hubzoid agents repo. Each subfolder is a hub.\n"
        "\n"
        "## Run a hub\n"
        "\n"
        "```bash\n"
        "python -m venv .venv && source .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        f"hubzoid run {hub_name}\n"
        "```\n"
        "\n"
        "## Add another hub\n"
        "\n"
        "```bash\n"
        "hubzoid init <hub-name>\n"
        "```\n"
        "\n"
        "Each hub gets its own `.env`, its own port, and its own user database.\n"
        "Agents are independent products.\n"
        "\n"
        "## Where the framework lives\n"
        "\n"
        "Installed from PyPI via `requirements.txt`. Framework source is at\n"
        "[github.com/hubzoid/hubzoid](https://github.com/hubzoid/hubzoid).\n"
    )
    return {
        parent / "requirements.txt": requirements_txt,
        parent / ".gitignore": gitignore,
        parent / "README.md": readme,
    }


def _template_root(name: str = "minimal") -> Path | None:
    """Return the on-disk path of a bundled template, or None.

    Templates live at `hubzoid/templates/<name>/`. The two shipped today
    are `minimal` (the runnable starter) and `demo` (the guided tour).
    """
    try:
        root = resources.files("hubzoid") / "templates" / name
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    # `resources.files` returns a Traversable; we need a real Path. For files
    # installed normally (not zipped), this just works.
    p = Path(str(root))
    return p if p.exists() and p.is_dir() else None


def _available_templates() -> list[str]:
    """List bundled template names. Used for error messages."""
    try:
        root = Path(str(resources.files("hubzoid") / "templates"))
    except (ModuleNotFoundError, FileNotFoundError):
        return []
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _owui_internal_port(ui_port: int) -> int:
    """Loopback port Open WebUI binds to when the edge router fronts it.

    The edge takes the public `ui_port`; OWUI moves here. Deterministic so
    ops can reason about it, overridable via HUBZOID_OWUI_PORT. The +40000
    offset keeps it clear of the operator's PORT range (typically ~3080).
    """
    override = os.environ.get("HUBZOID_OWUI_PORT")
    if override and override.isdigit() and 0 < int(override) <= 65535:
        return int(override)
    candidate = ui_port + 40000
    return candidate if candidate <= 65000 else ui_port + 1


def _wait_for(url: str, timeout: float = 60.0) -> bool:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _read_main_agent_name(hub: Path) -> str:
    """Pull the `name:` from AGENTS.md frontmatter to use as the model label."""
    from . import frontmatter as fm
    try:
        data, _ = fm.read(hub / "AGENTS.md")
        name = data.get("name", "agent")
        return _slugify(name)
    except Exception:  # noqa: BLE001
        return "agent"


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in str(text).strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"


if __name__ == "__main__":
    app()
