"""hubzoid CLI — typer app.

Commands:
  hubzoid init [PATH]    Scaffold a hub from the bundled template.
  hubzoid run [PATH]     Start FastAPI bridge + Open WebUI for a hub.
  hubzoid doctor [PATH]  Validate hub config and report issues.
  hubzoid test [PATH]    Send a hello prompt and assert non-empty response.
  hubzoid version        Print version.

Path defaults to `.` (the current directory) everywhere.
"""
from __future__ import annotations

import importlib.resources as resources
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
    help="Drop a folder of markdown files, get a chat agent with a polished web UI.",
)
console = Console()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Where to scaffold the hub. Default: current dir."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold a new hub from the bundled starter template."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)

    template_root = _template_root()
    if template_root is None:
        console.print("[red]Bundled template not found in the installed package.[/red]")
        raise typer.Exit(2)

    written: list[Path] = []
    skipped: list[Path] = []
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        dst = path / rel
        if dst.exists() and not force:
            skipped.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written.append(dst)

    console.print(f"[green]Initialized hub at[/green] {path}")
    if written:
        console.print(f"  wrote {len(written)} files")
    if skipped:
        console.print(f"  skipped {len(skipped)} existing files (use --force to overwrite)")
    console.print("\nNext:")
    console.print(f"  1. cp {path}/.env.example {path}/.env  # then add your API key")
    console.print(f"  2. edit {path}/AGENTS.md")
    console.print(f"  3. hubzoid run {path if path != Path.cwd() else '.'}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command()
def run(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    port: int = typer.Option(None, "--port", help="Open WebUI port. Default: 3080 (or PORT env)."),
    bridge_port: int = typer.Option(None, "--bridge-port", help="FastAPI bridge port. Default: 8000 (or BRIDGE_PORT env)."),
    no_ui: bool = typer.Option(False, "--no-ui", help="Skip Open WebUI; bridge only."),
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
    if not no_ui:
        try:
            from . import webui
            ui_proc = webui.start(
                hub_dir=hub,
                bridge_port=br_port,
                ui_port=ui_port,
                api_key=settings.first_api_key,
                model_label=settings.model_label or _read_main_agent_name(hub),
                webui_name=settings.webui_name,
            )
            log_path = getattr(ui_proc, "_log_path", None)
            console.print(f"[cyan]→ webui [/cyan]  http://127.0.0.1:{ui_port}  (booting, first start takes 1-2 min while it downloads its embedding model)")
            if log_path:
                console.print(f"            log: {log_path}")
        except FileNotFoundError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            console.print("Bridge only. Curl http://127.0.0.1:" + str(br_port) + "/v1/chat/completions to chat.")

    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down...[/cyan]")
        for p in (ui_proc, bridge_proc):
            if p is not None and p.poll() is None:
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # Block on the bridge process; its exit ends the CLI.
    try:
        bridge_proc.wait()
    finally:
        if ui_proc is not None and ui_proc.poll() is None:
            ui_proc.terminate()


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
        notes.append(f"no .env at {env_path} (copy .env.example -> .env and add a key)")

    # Try to actually build the runtime — this is the most thorough check.
    # Picks the backend based on MODEL (openai-agents by default,
    # claude-local when MODEL=claude-local).
    try:
        from . import runtime as runtime_lib
        rt = runtime_lib.build(hub)
        notes.append(f"runtime built: {rt.name!r} via {type(rt).__name__}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"runtime build failed: {type(exc).__name__}: {exc}")

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
    settings = settingslib.load(hub)
    if not settings.model:
        console.print("[red]MODEL is not set in .env. Cannot run test.[/red]")
        raise typer.Exit(2)

    from . import runtime as runtime_lib

    rt = runtime_lib.build(hub)
    console.print(f"[cyan]→[/cyan] {prompt}")
    text = asyncio.run(rt.run(prompt))
    console.print(f"[green]←[/green] {text}")


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
def _template_root() -> Path | None:
    """Return the on-disk path of the bundled starter template, or None."""
    try:
        root = resources.files("hubzoid") / "templates" / "starter"
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    # `resources.files` returns a Traversable; we need a real Path. For files
    # installed normally (not zipped), this just works.
    p = Path(str(root))
    return p if p.exists() else None


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
