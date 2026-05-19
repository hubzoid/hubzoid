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
    name: Path = typer.Argument(
        Path("demo-hub"),
        help="Name of the new hub folder. Created under the current directory. Default: demo-hub.",
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

    template_root = _template_root()
    if template_root is None:
        console.print("[red]Bundled template not found in the installed package.[/red]")
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

            # WEBUI_NAME cascade: operator .env -> MODEL_LABEL -> main
            # agent's name -> "Hubzoid" (final fallback so it never reads
            # as bare "Open WebUI" to a customer).
            resolved_webui_name = (
                settings.webui_name
                or settings.model_label
                or main_name
                or "Hubzoid"
            )

            ui_proc = webui.start(
                hub_dir=hub,
                bridge_port=br_port,
                ui_port=ui_port,
                api_key=settings.first_api_key,
                model_label=settings.model_label or main_name,
                webui_name=resolved_webui_name,
                suggestions=suggestions,
            )
            log_path = getattr(ui_proc, "_log_path", None)
            console.print(f"[cyan]→ webui [/cyan]  starting (first run takes 1-2 min while it downloads its embedding model)")
            if log_path:
                console.print(f"            log: {log_path}")
            if _wait_for(f"http://127.0.0.1:{ui_port}/", timeout=240):
                console.print(f"[green]→ webui [/green]  ready    http://127.0.0.1:{ui_port}")
            else:
                console.print(f"[yellow]→ webui [/yellow]  did not become ready in 4 min; check log above. URL: http://127.0.0.1:{ui_port}")
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
_STARTER_ENV = """\
# demo-hub configuration. This file is git-ignored.
#
# The default below uses your installed `claude` CLI and Pro/Max subscription
# for inference. No API key needed. Requires `claude login` already done.
#
# To use a hosted provider instead, comment out MODEL=claude-local and
# uncomment one of the alternative stanzas. Set the matching API key.

MODEL=claude-local
# MODEL=claude-local/sonnet     # pin Sonnet
# MODEL=claude-local/opus       # pin Opus
# MODEL=claude-local/haiku      # pin Haiku

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
WEBUI_NAME=Hubzoid Guide
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
# HTTP_ALLOWLIST=               # comma-separated hostnames the http_get tool may visit

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
