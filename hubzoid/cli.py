"""hubzoid CLI — typer app.

Commands:
  hubzoid init [PATH]              Scaffold a hub from the bundled template.
  hubzoid run [PATH]               Start a hub: bridge + web app on one port.
  hubzoid gateway [HUBS...]        One web app fronting many hub bridges.
  hubzoid schedule ...             Inspect / manually fire <hub>/schedule/*.md tasks.
  hubzoid slack run [PATH]         Start the Slack adapter (Socket Mode).
  hubzoid slack manifest [PATH]    Print a Slack App Manifest YAML.
  hubzoid slack systemd [PATH]     Print a systemd unit template.
  hubzoid doctor [PATH]            Validate hub config and report issues.
  hubzoid audit [PATH]             Show the access log (who called which tool).
  hubzoid test [PATH]              Send one prompt to the agent; --file attaches files.
  hubzoid version                  Print version.

Path defaults to `.` (the current directory) everywhere.
"""
from __future__ import annotations

import base64
import importlib.resources as resources
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import datadir

import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.markup import escape

from . import __version__
from . import settings as settingslib
from . import uploads as uploadslib

app = typer.Typer(
    name="hubzoid",
    add_completion=False,
    no_args_is_help=True,
    help="An open-source framework for production AI agents.",
)
console = Console()


def _print_version(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", callback=_print_version, is_eager=True,
                                 help="Print the installed hubzoid version and exit."),
) -> None:
    """An open-source framework for production AI agents."""


def _stop_processes(procs, *, timeout: float = 8.0) -> None:
    """Let child services close databases before the supervising process exits.

    In a container, exiting PID 1 kills its remaining children immediately.
    Signal all services first, then give them a shared, bounded grace period.
    """
    if os.name == "nt":
        _stop_groups(procs, timeout=timeout)
        return
    active = [p for p in procs if p is not None and p.poll() is None]
    for p in active:
        p.terminate()
    deadline = time.monotonic() + timeout
    for p in active:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _install_shutdown_handlers():
    def shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down...[/cyan]")
        # Unwind blocking calls before waiting in finally; waiting inside a
        # signal handler can re-enter Popen's non-reentrant lock.
        sys.exit(0)

    return (signal.signal(signal.SIGINT, shutdown), signal.signal(signal.SIGTERM, shutdown))


def _restore_shutdown_handlers(previous):
    signal.signal(signal.SIGINT, previous[0])
    signal.signal(signal.SIGTERM, previous[1])


def _load_settings(hub: Path):
    """settings.load for a command, turning an unreadable AWS secret into a
    clear message and exit status 1 (the message never carries a value)."""
    from .config_secrets import SecretFetchError

    try:
        return settingslib.load(hub)
    except SecretFetchError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _public_scheme(env: dict, *urls: str) -> str:
    """The public scheme ("https") the edge should assert as X-Forwarded-Proto.

    Derived from the operator's already-declared public URL (the explicit
    `--public-url`, then `HUBZOID_PUBLIC_URL`, which OAuth already
    requires) - so behind a real TLS proxy the edge advertises https with zero
    extra setup, while a plain-http localhost run declares nothing and is left
    untouched. Returns "" when no https public URL is known.
    """
    candidates = [u for u in urls if u] + [
        env.get("HUBZOID_PUBLIC_URL", ""),
    ]
    for url in candidates:
        if url.strip().lower().startswith("https://"):
            return "https"
    return ""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
def _available_local_models() -> list[str]:
    """Only authenticated CLIs are offered; never reads credentials into output."""
    available = []
    claude = shutil.which("claude")
    if claude:
        try:
            result = subprocess.run([claude, "auth", "status", "--json"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and json.loads(result.stdout).get("loggedIn") is True:
                available.append("claude-local")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    from .factory_codex import codex_available
    if codex_available():
        available.append("codex-local")
    return available


def _choose_initial_model() -> str | None:
    choices = _available_local_models()
    if not choices:
        return None
    if len(choices) == 1:
        console.print(f"Using authenticated local runtime: {choices[0]}")
        return choices[0]
    console.print("Available local runtimes:")
    for i, choice in enumerate(choices, 1):
        console.print(f"  {i}. {choice}")
    while True:
        selected = typer.prompt("Choose the runtime for this hub", default=1, type=int)
        if 1 <= selected <= len(choices):
            return choices[selected - 1]
        console.print("Choose one of the listed numbers.")


DEFAULT_TEMPLATE = "operations"

# Hosted model providers `hubzoid init` sets up from a pasted key: (name, the
# .env setting, default model, key prefix). The models match the commented
# stanzas in the starter .env.
_KEY_PROVIDERS = (
    ("OpenRouter", "OPENROUTER_API_KEY", "openrouter/anthropic/claude-haiku-4.5", "sk-or-"),
    ("Anthropic", "ANTHROPIC_API_KEY", "anthropic/claude-haiku-4-5", "sk-ant-"),
    ("OpenAI", "OPENAI_API_KEY", "openai/gpt-4o-mini", "sk-"),
)
# One line, nothing a .env file would read as a comment, quote or second value.
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_.\-]{20,512}$")


def _provider_for_key(key: str):
    """The provider a key belongs to, by its prefix: an entry of
    _KEY_PROVIDERS, "subscription" for a Claude subscription token, or None."""
    if key.startswith("sk-ant-oat"):
        return "subscription"
    for provider in _KEY_PROVIDERS:
        if key.startswith(provider[3]):
            return provider
    return None


def _prompt_for_key() -> tuple[str, str, str, str] | None:
    """Offer to save a hosted model key when no Claude or Codex CLI is signed in.
    Returns (provider name, .env setting, key, model), or None to skip. Called
    only in an interactive session; the key is never echoed or logged."""
    console.print(
        "\nNo signed-in Claude Code or Codex CLI was found, so this hub needs a model.\n"
        "Paste an OpenRouter, Anthropic or OpenAI API key to save it in the hub's .env\n"
        "(readable only by you), or press Enter to skip and set one later."
    )
    for _attempt in range(3):
        key = typer.prompt("API key", default="", show_default=False, hide_input=True).strip()
        if not key:
            return None
        if not _KEY_SHAPE.match(key):
            console.print("That does not look like an API key. Paste the whole key, or press Enter to skip.")
            continue
        provider = _provider_for_key(key)
        if provider == "subscription":
            console.print("That is a Claude subscription token (from `claude setup-token`), not an API key. "
                          "It works with the claude CLI: keep MODEL=claude-local and set "
                          "CLAUDE_CODE_OAUTH_TOKEN in .env. Paste an API key, or press Enter to skip.")
            continue
        if provider is None:
            console.print("Which provider is this key for?")
            for i, entry in enumerate(_KEY_PROVIDERS, 1):
                console.print(f"  {i}. {entry[0]}")
            choice = typer.prompt("Provider", default=1, type=int)
            if not 1 <= choice <= len(_KEY_PROVIDERS):
                console.print("Choose one of the listed numbers.")
                continue
            provider = _KEY_PROVIDERS[choice - 1]
        name, setting, model_id, _prefix = provider
        return name, setting, key, model_id
    return None


def _interactive() -> bool:
    """A person at a terminal, who can answer a prompt."""
    return sys.stdin.isatty()


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, from the first byte (it holds keys)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o600)  # an existing file keeps its mode through O_TRUNC


@app.command()
def init(
    name: Path = typer.Argument(
        Path("demo-hub"),
        help="Name of the new hub folder. Created under the current directory. Default: demo-hub.",
    ),
    template: str = typer.Option(
        DEFAULT_TEMPLATE,
        "--template", "-t",
        help="Which bundled template to use. 'operations' (default): an operations "
        "assistant for a fictional home-goods shop, with policy files, a stock export, "
        "a stock_check tool and three suggested prompts. 'minimal': a tiny, runnable hub "
        "with one example of each file type. 'demo': the guided tour with a Hubzoid Guide "
        "agent, four teaching skills and six knowledge pages. 'watchtower': a "
        "workflow-first sample, a scheduled check on bundled sample metrics that explains "
        "threshold breaches. 'gitzoid': PR reviews, security checks and engineering reports, with a fictional repo.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files in the hub folder."),
    model: str | None = typer.Option(None, "--model", help="Model for a new hub, e.g. codex-local, claude-local or a provider model id."),
) -> None:
    """Scaffold a new hub. Also drops agents-repo wrapper files if the parent looks fresh.

    First run in an empty directory:
      $ hubzoid init devops-agent
      → writes ./requirements.txt, ./.gitignore, ./README.md, ./devops-agent/...

    Second run in the same directory:
      $ hubzoid init sales-agent
      → writes ./sales-agent/... only. Parent files are left alone.

    A tiny hub with one example of each file type, or the guided tour:
      $ hubzoid init my-hub --template minimal
      $ hubzoid init my-hub --template demo

    In a terminal with no signed-in Claude Code or Codex CLI, init offers to
    save an OpenRouter, Anthropic or OpenAI key in the hub's .env.

    The result is a multi-hub agents repo built one hub at a time.
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

    fresh_hub = not (hub_dir / "AGENTS.md").exists()
    hub_dir.mkdir(parents=True, exist_ok=True)

    # 1. Scaffold the hub folder from the bundled template.
    written: list[Path] = []
    skipped: list[Path] = []
    for src in template_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(template_root)
        # Never copy runtime state (a template used for a local run or test can
        # hold .hubzoid/, including the artifact link secret).
        if rel.parts[0] in (".hubzoid", ".openwebui-data") or "__pycache__" in rel.parts:
            continue
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
    selected_model = None
    saved_key = None
    if env_dst.exists() and not force:
        skipped.append(env_dst)
    else:
        import secrets as _secrets

        selected_model = model
        if not model and fresh_hub and _interactive():
            # A person at a terminal: use a signed-in Claude Code or Codex CLI,
            # else offer to save a hosted provider key.
            selected_model = _choose_initial_model()
            if selected_model is None:
                saved_key = _prompt_for_key()
                if saved_key:
                    selected_model = saved_key[3]
        # Not a terminal (scripts, CI, image builds): never prompt. The .env keeps
        # MODEL=claude-local and the commented provider stanzas below it, as
        # before; choose another model with --model or by editing the .env.
        starter = _STARTER_ENV
        if selected_model:
            if any(c in selected_model for c in "\n\r#"):
                raise typer.BadParameter("Model must be a single model identifier.")
            model_line = f"MODEL={selected_model}"
            if saved_key:
                model_line += f"\n{saved_key[1]}={saved_key[2]}"
            starter = starter.replace(_STARTER_MODEL_LINE, model_line)
        _write_private(env_dst, starter.replace(
            _STARTER_BRIDGE_KEY_LINE,
            f"BRIDGE_API_KEYS={_secrets.token_urlsafe(24)}  # random per hub; comma-separated keys for the /v1 API",
        ))
        written.append(env_dst)
        if saved_key:
            console.print(f"Saved the {saved_key[0]} key in {env_dst} (MODEL={selected_model}).")

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
    step = 1
    if not selected_model:
        console.print(f"  {step}. Check the model in {escape(str(env_dst))}: MODEL=claude-local uses a "
                      "signed-in `claude` CLI. Without one, set an OpenRouter, Anthropic or OpenAI key there.")
        step += 1
    console.print(f"  {step}. hubzoid run {escape(shlex.quote(str(hub_dir)))}   (opens the web app in your browser)")
    if template == "gitzoid":
        target = escape(shlex.quote(str(hub_dir)))
        console.print(
            f"\nGitZoid starts with a fictional repo and preview publishing. Your selected model is still required.\n"
            f"  hubzoid schedule run {target} setup_check\n"
            f"  hubzoid schedule run {target} repo_profiles\n"
            f"  hubzoid schedule run {target} pr_sweep\n"
            f"Connect your repos: python {escape(shlex.quote(str(hub_dir / 'setup.py')))} --help\n"
            "Enable GitZoid tools for your own account in Console → Access (or use hubzoid grant).\n"
            "All eligible open PRs are queued by default; drafts and bot PRs can be included. See the Hub README."
        )
    if template == DEFAULT_TEMPLATE:
        console.print(
            "\n[dim]Kestrel & Oak is a fictional shop. Try a suggested prompt, such as "
            "\"What should we reorder today, and what could run out first?\"\n"
            "Other templates: --template minimal (one example of each file type), "
            "--template demo (a guided tour of Hubzoid).[/dim]"
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command()
def run(
    hubs: list[Path] = typer.Argument(None, help="Hub folders. Default: the deployment in hubzoid.yaml here, "
                                                 "else the hub in this folder."),
    port: int = typer.Option(None, "--port", help="Public port: the web app, downloads and MCP. Default: 3080 (or PORT env)."),
    bridge_port: int = typer.Option(None, "--bridge-port", hidden=True, help="FastAPI bridge port on 127.0.0.1. Default: 8000 (or BRIDGE_PORT env)."),
    data_dir: Path = typer.Option(None, "--data-dir", help="Where a deployment of several hubs keeps its state "
                                                           "(manifest, shared database, keys, logs). Default: "
                                                           ".hubzoid next to hubzoid.yaml."),
    host: str = typer.Option("127.0.0.1", "--host", envvar="HUBZOID_HOST", help="Interface the public port binds to (or HUBZOID_HOST env). 0.0.0.0 exposes it on the network, which needs sign-in (HUBZOID_AUTH=true). The bridge always stays on 127.0.0.1."),
    no_ui: bool = typer.Option(False, "--no-ui", hidden=True, help="One hub's own process under a supervisor."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the web app in a browser. It opens only from a terminal, on a loopback host."),
    slack: bool = typer.Option(
        False,
        "--slack", "-s",
        hidden=True, help="Also start the Slack adapter (Socket Mode). Reads SLACK_BOT_TOKEN and "
        "SLACK_APP_TOKEN from .env. Soft-fails with a warning if either is missing.",
    ),
    whatsapp: bool = typer.Option(
        False,
        "--whatsapp",
        hidden=True, help="Also start the WhatsApp webhook surface. Reads WHATSAPP_* from .env and "
        "exposes /webhooks/whatsapp via the edge. Soft-skips if unconfigured.",
    ),
    telegram: bool = typer.Option(
        False,
        "--telegram",
        hidden=True, help="Also start the Telegram webhook surface. Reads TELEGRAM_* from .env and "
        "exposes /webhooks/<hub>/telegram via the edge. Soft-skips if unconfigured.",
    ),
    webhook: bool = typer.Option(
        False,
        "--webhook",
        hidden=True, help="Also start the generic webhook surface (alerting, CI, automations). "
        "Reads WEBHOOK_INBOUND_SECRET/_NAME/_HMAC from .env and exposes "
        "/webhooks/<hub>/<name> via the edge. Soft-skips if unconfigured.",
    ),
) -> None:
    """Start a deployment: the web app on one public port and every hub in its
    own supervised process, restarted on its own if it stops.

    `hubzoid run` runs hubzoid.yaml in this folder (or the hub in this
    folder); `hubzoid run ./a ./b` runs those hubs. Control a running
    deployment with `hubzoid status`, `restart`, `stop`, `start`, `reload` and
    `logs`.

    Sign-in is off by default (local mode: you are the hub's owner), so the
    public port stays on this machine. Turn sign-in on (`sign_in: true`, or
    HUBZOID_AUTH=true) before binding another interface.
    """
    from . import deployfile

    targets = [Path(h) for h in (hubs or [])]
    df = None
    if not targets or (len(targets) == 1 and deployfile.find(targets[0])
                       and not (targets[0] / "AGENTS.md").is_file()):
        found = deployfile.find(targets[0] if targets else Path.cwd())
        if found is not None:
            try:
                df = deployfile.load(found)
            except deployfile.DeployFileError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(2)
            if not df.hubs:
                console.print(f"[red]{df.path} lists no hubs.[/red] Add their folders under `hubs:`.")
                raise typer.Exit(2)
            _check_deployment_sections(df)
            targets = df.hub_dirs()
        elif not targets:
            targets = [Path(".")]
    if len(targets) > 1 or (df is not None and targets[0].resolve() != df.root):
        if no_ui:
            console.print("[red]--no-ui runs one hub.[/red]")
            raise typer.Exit(2)
        _refuse_channel_flags(slack, whatsapp, telegram, webhook)
        _run_deployment(targets, port=port, host=host, public_url=None, name="Hubzoid",
                        data_dir=data_dir, df=df)
        return
    if df is not None:
        values, problems = deployfile.environment(df)
        if problems:
            for problem in problems:
                console.print(f"[red]{escape(problem)}[/red]")
            raise typer.Exit(2)
        os.environ.update(values)
    hub = targets[0].resolve()
    if not no_ui:
        _refuse_channel_flags(slack, whatsapp, telegram, webhook)
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
        console.print(f"[red]No AGENTS.md in {hub}.[/red]\n"
                      "Create AGENTS.md with the hub agent's instructions, or use "
                      "`hubzoid init` for a starter hub. See docs/authoring.md.")
        raise typer.Exit(2)

    settings = _load_settings(hub)
    ui_port = port or settings.ui_port
    br_port = bridge_port or settings.bridge_port
    from . import appmode, config_secrets

    # Open WebUI settings that decide sign-in or the interface, and settings
    # that disagree with the gateway's deployment (a bridge started on its own,
    # `gateway --no-bridges`, may not share its environment), stop it here.
    conflicts = appmode.deployment_conflicts(hub)
    if conflicts:
        for problem in conflicts:
            console.print(f"[red]{escape(problem)}[/red]")
        raise typer.Exit(2)

    _warn_retired(hub)
    auth_on = appmode.auth_enabled(hub)
    if not no_ui:
        _refuse_unauthenticated_network(hub, host, auth_on)
    _check_openwebui_upgrade(hub, auth_on)

    # A health response from an already running hub is not readiness for this
    # child. Refuse occupied ports before starting anything or routing to it.
    _ensure_port_available("127.0.0.1", br_port, "Bridge", "--bridge-port")
    if not no_ui:
        if br_port == ui_port:
            console.print("[red]Bridge and public ports must be different.[/red]")
            raise typer.Exit(2)
        _ensure_port_available(host, ui_port, "Public", "--port")

    # 1. Start the bridge in a subprocess. We pass HUBZOID_HUB_DIR via env so
    #    `hubzoid.server.build_app` knows what to load. The bridge (and the
    #    Slack and inbound children) load the hub secret and restricted layers
    #    themselves, so they start from the environment without them.
    bridge_env = config_secrets.for_hub_children(os.environ)
    bridge_env["HUBZOID_HUB_DIR"] = str(hub)
    # Tell the bridge process its REAL port. Uvicorn binds it via --port, but
    # settings.load() inside the bridge reads BRIDGE_PORT from env (else defaults
    # to 8000). The OTel normalize intercept and the HUBZOID_PUBLIC_URL artifact
    # fallback both need settings.bridge_port to match the actual bind.
    bridge_env["BRIDGE_PORT"] = str(br_port)
    mcp_url = None
    if not no_ui:
        # Links the bridge writes (downloads) go through the public port people
        # open, not the bridge's own loopback port.
        bridge_env.update(_origin_defaults(os.environ, host, ui_port))
        # Hosted MCP is on by default where its OAuth can work: a local run, or an
        # https public URL. A key set in the hub's .env or the environment wins.
        mcp_env = _mcp_defaults(os.environ, _public_origin(host, ui_port))
        bridge_env.update(mcp_env)
        if settingslib.truthy(bridge_env.get("MCP_SERVER")):
            mcp_url = (bridge_env.get("MCP_PUBLIC_URL") or "").strip() or None
    bridge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.server:build_app", "--factory",
        "--host", "127.0.0.1", "--port", str(br_port),
        "--log-level", settings.log_level,
    ]
    if no_ui:
        _run_hub_process(hub, bridge_cmd, bridge_env, br_port=br_port)
        return

    # One hub is a deployment of one: the supervisor runs its bridge (and its
    # 1.x channels) as one unit and the edge as another, restarts either on its
    # own, and answers `hubzoid status|restart|...` (hubzoid.supervisor).
    from . import supervisor as sup_lib

    state = datadir.state(hub)
    from . import db as dblib

    _migrate_first(dblib.operational_url(hub))
    key = _deployment_bridge_key(state) if settings.first_api_key == "dev" else ""
    unit_key = settings.first_api_key
    if key and not (os.environ.get("BRIDGE_API_KEYS") or "").strip():
        bridge_env["BRIDGE_API_KEYS"] = unit_key = key
    channel_env = dict(bridge_env)
    specs = [sup_lib.Spec(bridge_cmd, bridge_env)] + _channel_specs(hub, channel_env)
    from .inbound.routing import hub_slug

    slug = hub_slug(hub, os.environ)
    unit = sup_lib.Unit(name=hub.name.lower(), slug=slug, hub_dir=hub, specs=specs,
                        health_url=f"http://127.0.0.1:{br_port}/healthz",
                        bridge_url=f"http://127.0.0.1:{br_port}", key=unit_key)
    edge_spec, routes = _web_app_edge_spec(hub, settings, host=host, ui_port=ui_port, br_port=br_port,
                                           inbound=any(s.role == "channel" and "inbound" in s.cmd for s in specs),
                                           slug=slug, state=state)
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host.strip("[]")
    probe = f"[{probe}]" if ":" in probe else probe
    edge = sup_lib.Unit(name="edge", kind="edge", specs=[edge_spec],
                        health_url=f"http://{probe}:{ui_port}/healthz")
    sup = _supervisor([unit, edge], state, table={
        "default": f"http://127.0.0.1:{br_port}", "default_fallbacks": [], "routes": routes})
    previous = _install_shutdown_handlers()
    try:
        console.print(f"[cyan]→ bridge[/cyan]  http://127.0.0.1:{br_port}  (hub: {hub.name})")
        if not sup.start_all([unit]).get(unit.name):
            console.print(f"[red]bridge failed to come up[/red]  ({escape(unit.error or unit.last_exit)}). "
                          f"It is tried again; `hubzoid logs {unit.name}` shows why.")
        else:
            console.print("[green]→ bridge[/green]  ready")
        if not sup.start(edge).get("ok"):
            console.print(f"[red]The web app could not open port {ui_port} on {escape(host)}. Is another "
                          "program using it? Choose another with --port.[/red]")
            raise typer.Exit(1)
        url = appmode.public_url() or _local_url(host, ui_port)
        mode = "sign-in on" if auth_on else "local mode, sign-in off"
        # One line each, never wrapped, so the URL and the command copy cleanly
        # from any terminal or log viewer.
        console.print(f"[bold green]✓ Hubzoid is ready:[/bold green] {escape(url)}  [dim]({mode})[/dim]",
                      soft_wrap=True)
        if mcp_url:
            console.print(f"  [dim]Connect Claude Code:[/dim] claude mcp add --transport http "
                          f"{escape(_mcp_client_name(hub))} {escape(mcp_url)}", soft_wrap=True)
        if not appmode.public_url() and not appmode.is_loopback_host(host):
            console.print("  [dim]Set HUBZOID_PUBLIC_URL to the address people open, so download "
                          "links and sign-in redirects use it.[/dim]")
        if _should_open_browser(host, no_open):
            _open_browser(url)
        _wait_any(unit.procs + edge.procs, supervisor=sup)
    finally:
        try:
            _stop_groups([p for p in (edge.procs + unit.procs)[::-1]])
        finally:
            _restore_shutdown_handlers(previous)


def _check_deployment_sections(df) -> None:
    """A deployment file whose brand, chat or diagnostics section is invalid
    stops the start: those hold policy (terms, sharing, exports, storage)
    that must never fall back to an open default."""
    problems = []
    if df.chat is not None:
        from .chat import config as chat_cfg

        try:
            chat_cfg.validate_document({"version": 1, **df.chat})
        except chat_cfg.ConfigError as exc:
            problems.append(f"chat: {exc.message}")
    if df.brand is not None:
        from .ui import branding_models

        try:
            branding_models.validate({"version": 1, **df.brand})
        except branding_models.BrandError as exc:
            problems.append(f"brand: {exc.message}")
    if df.diagnostics is not None:
        from .admin import config as admin_cfg

        try:
            admin_cfg.validate({"version": 1, "diagnostics": df.diagnostics})
        except admin_cfg.AdminError as exc:
            problems.append(f"diagnostics: {exc.message}")
    for problem in problems:
        console.print(f"[red]{escape(str(df.path))}, {escape(problem)}[/red]")
    if problems:
        raise typer.Exit(2)


def _refuse_channel_flags(slack: bool, whatsapp: bool, telegram: bool, webhook: bool) -> None:
    """--slack and friends ran a 1.x channel next to one hub. Channels turn on
    by a file in the hub's channels/ folder now; say which file to add."""
    from .channels import legacy

    asked = [p for p, on in (("slack_assistant", slack), ("whatsapp_inbound", whatsapp), ("telegram", telegram),
                             ("webhook", webhook)) if on is True]
    if not asked:
        return
    console.print("[red]Channels turn on by a file now, not a flag.[/red] Add to the hub's channels/ folder:")
    for provider in asked:
        console.print(f"\n  channels/{legacy.SUGGESTED[provider]}.yaml\n"
                      + "".join(f"    {line}\n" for line in legacy.file_text(provider).splitlines()), highlight=False)
    console.print("Its secrets stay in the hub's .env. See docs/channels.md.")
    raise typer.Exit(2)


def _run_hub_process(hub: Path, bridge_cmd, bridge_env, *, br_port: int) -> None:
    """One hub's own process under a multi-hub supervisor (`run --no-ui`): its
    bridge, ending when the bridge ends so the supervisor restarts the hub."""
    from . import supervisor as sup_lib

    sup_lib.watch_parent()
    children: list = []
    previous = _install_shutdown_handlers()
    try:
        console.print(f"[cyan]→ bridge[/cyan]  http://127.0.0.1:{br_port}  (hub: {hub.name})")
        bridge_proc = subprocess.Popen(bridge_cmd, env=bridge_env, start_new_session=False)
        children.append(bridge_proc)
        if not _wait_for_bridge(bridge_proc, f"http://127.0.0.1:{br_port}/healthz"):
            console.print("[red]bridge failed to come up[/red]")
            raise typer.Exit(1)
        console.print("[green]→ bridge[/green]  ready")
        _wait_any(children)
    finally:
        try:
            _stop_processes(children[::-1])
        finally:
            _restore_shutdown_handlers(previous)


# ---------------------------------------------------------------------------
# run: helpers for the Hubzoid web app (default mode)
# ---------------------------------------------------------------------------
def _ensure_port_available(host: str, port: int, label: str, option: str) -> None:
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            # Match uvicorn: closing TCP connections must not block a restart.
            # This still refuses a port with an active listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError:
        console.print(f"[red]{label} port {port} is unavailable on {escape(host)}.[/red]\n"
                      f"Choose another port with {option}; an existing service was left running.")
        raise typer.Exit(1)


def _wait_for_bridge(proc, url: str, timeout: float = 180.0, *,
                     expected_hub: str | None = None, expected_model: str | None = None,
                     label: str = "bridge") -> bool:
    """Wait for the bridge's health check. Stops at once if the bridge exits,
    and is patient otherwise: a first start on a slow or busy machine creates
    databases and loads the agent runtime."""
    deadline = time.monotonic() + timeout
    noted = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        identity = {"expected_hub": expected_hub, "expected_model": expected_model} if expected_hub else {}
        if _wait_for(url, timeout=5.0, **identity):
            return proc.poll() is None
        if proc.poll() is not None:
            return False
        if not noted and time.monotonic() - started > 20:
            console.print(f"[dim]→ {label}  still starting[/dim]")
            noted = True
    return False


_MCP_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}  # as mcp_oauth.validate_public_url


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _url_host(host: str) -> str:
    """`host` as it appears in a URL: a wildcard bind becomes 127.0.0.1 (this
    machine's address for it), and an IPv6 literal gets brackets."""
    h = (host or "").strip() or "0.0.0.0"
    if h in ("0.0.0.0", "::"):
        h = "127.0.0.1"
    return f"[{h}]" if ":" in h and not h.startswith("[") else h


def _local_url(host: str, port: int) -> str:
    return f"http://{_url_host(host)}:{port}"


def _public_origin(host: str, port: int) -> str:
    """Where browsers and MCP clients reach this hub: the configured public URL's
    origin, else this machine's own address for a loopback bind. Empty when the
    port is bound to the network with no public URL configured (unknown)."""
    from . import appmode

    configured = appmode.public_url()
    if configured:
        return appmode.normalize_origin(configured)
    return _local_url(host, port) if appmode.is_loopback_host(host) else ""


def _origin_defaults(env, host: str, port: int) -> dict[str, str]:
    """HUBZOID_PUBLIC_URL and HUBZOID_ALLOWED_ORIGINS for the bridge when no
    public URL is configured and the web app is on this machine.

    The bridge builds download links from HUBZOID_PUBLIC_URL, falling back to
    its own loopback port. Setting it to the public port's address sends links
    through the edge, as people open the page. The other local spelling
    (localhost or 127.0.0.1) stays an allowed origin, so a page opened either
    way may send changes. A configured public URL, or a network bind (whose
    address only the operator knows), is left alone."""
    from . import appmode

    if appmode.public_url(env) or not appmode.is_loopback_host(host):
        return {}
    origin = _local_url(host, port)
    allowed = [o.strip() for o in (env.get("HUBZOID_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    for spelling in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        if spelling != origin and spelling not in allowed:
            allowed.append(spelling)
    return {"HUBZOID_PUBLIC_URL": origin, "HUBZOID_ALLOWED_ORIGINS": ",".join(allowed)}


def _mcp_defaults(env, origin: str) -> dict[str, str]:
    """MCP_SERVER and MCP_PUBLIC_URL to add to the bridge's environment.

    Hosted MCP signs people in with OAuth, which needs an https public URL or a
    loopback one. So MCP is on by default when the public origin is either, at
    `<origin>/mcp`. A key already set (the hub's .env, a secret or the process
    environment, even when empty) is explicit and wins: the bridge re-reads the
    hub's .env, so run and bridge agree."""
    from urllib.parse import urlsplit

    out: dict[str, str] = {}
    parts = urlsplit(origin) if origin else None
    usable = bool(parts) and (parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in _MCP_LOOPBACK_HOSTS))
    if "MCP_SERVER" not in env and usable:
        out["MCP_SERVER"] = "true"
    on = settingslib.truthy(out.get("MCP_SERVER", env.get("MCP_SERVER")))
    if on and usable and not (env.get("MCP_PUBLIC_URL") or "").strip():
        out["MCP_PUBLIC_URL"] = origin.rstrip("/") + "/mcp"
    return out


def _mcp_client_name(hub: Path) -> str:
    name = _read_main_agent_name(hub)
    return _slugify(hub.name) if name == "agent" else name


def _warn_retired(hub: Path) -> None:
    """Name each Open WebUI setting still present that does not stop a start
    (the ones that do are in ``appmode.deployment_conflicts``)."""
    from . import appmode

    for r in appmode.retired_settings(hub):
        if not r.fatal:
            console.print(f"[yellow]{escape(Path(hub).name)}: {escape(r.name)} is an Open WebUI setting "
                          f"Hubzoid 2.0 no longer uses. {escape(r.advice)}[/yellow]")


def _refuse_unauthenticated_network(hub: Path, host: str, auth_on: bool) -> None:
    """Local mode (sign-in off) makes every visitor the hub's owner, so its
    public port stays on loopback unless the operator explicitly accepts the risk."""
    from . import appmode

    if auth_on or appmode.is_loopback_host(host):
        return
    if settingslib.truthy(os.environ.get("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK")):
        console.print(f"[yellow]Sign-in is off and the web app listens on {escape(host)}: anyone who can "
                      "reach this port acts as the hub's owner "
                      "(HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true).[/yellow]")
        return
    console.print(
        f"[red]Sign-in is off, so listening on {escape(host)} (--host or HUBZOID_HOST) would let "
        "anyone who can reach this port use the hub as its owner.[/red]\n"
        f"Turn sign-in on: set HUBZOID_AUTH=true in {escape(str(hub / '.env'))} and create the first\n"
        "administrator with HUBZOID_ADMIN_EMAIL and HUBZOID_ADMIN_PASSWORD (see `hubzoid admin --help`).\n"
        "Or keep the web app on this machine: --host 127.0.0.1.\n"
        "To run without sign-in on a network you trust: HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true."
    )
    if _in_container():
        console.print("In Docker, to try it without sign-in, publish the port on this machine only "
                      "(-p 127.0.0.1:3080:3080) and set HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true.")
    raise typer.Exit(2)


def _check_openwebui_upgrade(hub: Path, auth_on: bool) -> None:
    """Upgrading from 1.0.x: an Open WebUI install with no Hubzoid accounts yet.

    With sign-in on, starting would lock everyone out, so stop with the two ways
    forward. In local mode there is nothing to sign in to; say that the old
    chats can be imported. Hubzoid accounts that cannot be read are not zero
    accounts: with sign-in on, stop and say why instead."""
    from . import upgrade

    found = upgrade.openwebui_accounts(hub)
    if found is None:
        return
    where, people = found
    quoted = escape(shlex.quote(str(hub)))
    try:
        if upgrade.hubzoid_accounts(hub):
            return
    except upgrade.AccountsUnreadable as exc:
        reason = escape(str(exc))
        if not auth_on:  # nothing to lock out: the bridge reports the store
            console.print(f"[yellow]The Hubzoid accounts could not be read ({reason}), so this start "
                          f"cannot tell whether chats from Open WebUI ({escape(where)}) were imported. "
                          f"`hubzoid doctor {quoted}` shows the cause.[/yellow]")
            return
        console.print(
            f"[red]This hub has an Open WebUI database with {people} account(s) ({escape(where)}), "
            f"and the Hubzoid accounts could not be read ({reason}).[/red]\n"
            "Starting with sign-in on could lock everyone out, so the start stops here.\n"
            "Check the operational database (HUBZOID_OPERATIONAL_DB, or DATABASE_URL in "
            f"{escape(str(hub / '.env'))}).\n"
            f"`hubzoid doctor {quoted}` shows the cause. Then start again."
        )
        raise typer.Exit(1)
    if not auth_on:
        console.print(f"[dim]Chats from Open WebUI ({escape(where)}) can be imported: "
                      f"hubzoid migrate openwebui {quoted} (back up first)[/dim]")
        return
    console.print(
        f"[red]This hub has an Open WebUI database with {people} account(s) ({escape(where)}), "
        "but no Hubzoid accounts yet.[/red]\n"
        "Starting with sign-in on would lock everyone out, so the start stops here.\n"
        "Hubzoid now has its own web app and sign-in. Choose one:\n"
        f"  1. Move accounts, groups and chats:  hubzoid migrate openwebui {quoted}\n"
        "     Do a dry run first (see `hubzoid migrate openwebui --help`), then back up\n"
        f"     (hubzoid backup {quoted}), stop the hub, and apply.\n"
        "  2. Keep Open WebUI as the chat app: stay on Hubzoid 1.x (pip install \"hubzoid<2\")."
    )
    raise typer.Exit(1)


def _web_app_edge_spec(hub: Path, settings, *, host: str, ui_port: int, br_port: int,
                       inbound: bool, slug: str, state: Path):
    """The public port for the Hubzoid web app of one hub: every path goes to
    the bridge, `/b/<slug>/...` too (the same addresses as in a deployment of
    many), and `/webhooks/<hub>` to the inbound process. Returns the edge's
    Spec and its routes."""
    from . import config_secrets
    from . import supervisor as sup_lib

    edge_env = config_secrets.deployment_view(os.environ)
    edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{br_port}"
    edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env)
    edge_routes = []
    if inbound:
        # Inbound surfaces receive on a loopback port; only /webhooks/<hub> is
        # exposed (each POST is signature-, secret- or HMAC-verified first).
        from .inbound.routing import inbound_port

        edge_routes.append({"prefix": f"/webhooks/{slug}",
                            "upstream": f"http://127.0.0.1:{inbound_port(os.environ)}", "hub": slug})
    from .workflows.events import route_declarations
    for name in route_declarations(hub):
        edge_routes.insert(0, {"prefix": f"/webhooks/{slug}/{name}",
                               "upstream": f"http://127.0.0.1:{br_port}", "hub": slug})
    base = f"/b/{slug}"
    for part in ("/artifacts", "/portal/api", "/api", "/branding", "/v1"):
        edge_routes.append({"prefix": base + part, "upstream": f"http://127.0.0.1:{br_port}",
                            "strip_prefix": base, "hub": slug})
    edge_env["HUBZOID_EDGE_WORKFLOW_HUBS"] = json.dumps([str(hub)])
    edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(edge_routes)
    edge_env["HUBZOID_EDGE_TABLE"] = str(Path(state) / "routes.json")
    edge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.edge:_factory", "--factory",
        "--host", host, "--port", str(ui_port),
        "--log-level", settings.log_level,
    ]
    return sup_lib.Spec(edge_cmd, edge_env, role="edge"), edge_routes


def _should_open_browser(host: str, no_open: bool) -> bool:
    """Open the web app only for a person at this machine: a terminal, a loopback
    bind, and a graphical session (a console browser would take over the terminal)."""
    from . import appmode

    if no_open is not False or not sys.stdout.isatty() or not appmode.is_loopback_host(host):
        return False
    if os.environ.get("BROWSER"):
        return True
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - opening a browser is a convenience only
        pass


# ---------------------------------------------------------------------------
# gateway — one chat app fronting many hub bridges
# ---------------------------------------------------------------------------
@app.command()
def gateway(
    hubs: list[Path] = typer.Argument(..., help="Hub directories to serve behind one front door."),
    port: int = typer.Option(None, "--port", help="Public port people reach the deployment on. Default: 3080 (or PORT env)."),
    host: str = typer.Option("127.0.0.1", "--host", envvar="HUBZOID_HOST", help="Interface the public edge binds to (or HUBZOID_HOST env). Use 0.0.0.0 to expose."),
    public_url: str = typer.Option(None, "--public-url", help="Public base URL (e.g. https://hub.example.com); used to build per-hub artifact download links. Falls back to HUBZOID_PUBLIC_URL."),
    name: str = typer.Option("Hubzoid", "--name", help="The deployment's display name."),
    data_dir: Path = typer.Option(None, "--data-dir", help="Gateway state dir: manifest, shared database and deployment key. Default: ./.hubzoid-gateway."),
    launch_bridges: bool = typer.Option(True, "--launch-bridges/--no-bridges", help="Launch each hub's headless bridge. --no-bridges fronts bridges already running as separate units."),
) -> None:
    """Serve many hubs behind one front door — one headless bridge per hub.

    N bridges and one edge serving the Hubzoid web app. Every bridge shares
    the operational store (accounts, sessions, conversations, access), so one
    sign-in covers every agent the person may use. The edge sends
    /b/<slug>/api, /artifacts, /mcp and /branding to that hub's bridge and
    everything else to the first bridge. The page chrome comes from
    HUBZOID_GATEWAY_BRANDING (a hub slug or a path), <data-dir>/branding/, or
    the first hub's branding/. Sign-in is set once, in the gateway's
    environment (HUBZOID_AUTH); a hub .env that disagrees stops the gateway.
    Grant people agent and tool access in Settings. See docs/DEPLOYING.md.
    """
    if not launch_bridges:
        console.print("[red]--no-bridges is gone: `hubzoid run` supervises every hub and restarts one "
                      "with `hubzoid restart <hub>`. See docs/DEPLOYING.md (one service).[/red]")
        raise typer.Exit(2)
    from . import deployfile

    console.print("[yellow]`hubzoid gateway` is now `hubzoid run`[/yellow] (this alias goes in the next "
                  "release). The same deployment as a file, to save as hubzoid.yaml next to the hubs "
                  "and start with `hubzoid run`:")
    cwd = Path.cwd().resolve()
    rel = [os.path.relpath(h.resolve(), cwd) for h in hubs]
    console.print(escape(deployfile.example(
        hubs=rel, public_url=(public_url or os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/") or None,
        name=None if name == "Hubzoid" else name, sign_in=None, owner=None)), highlight=False)
    _run_deployment(hubs, port=port, host=host, public_url=public_url, name=name, data_dir=data_dir)


def _run_deployment(hubs: list[Path], *, port: int | None, host: str, public_url: str | None,
                    name: str, data_dir: Path | None, df=None) -> None:
    """A deployment of one or more hubs, each its own supervised unit behind
    one edge, sharing one operational store (what `hubzoid gateway` ran)."""
    from . import deployfile  # noqa: F401 - used when df is given
    from . import gateway as gateway_lib

    hub_dirs = [h.resolve() for h in hubs]
    names = [h.name.strip().lower() for h in hub_dirs]
    if len(names) != len(set(names)) or '*' in names:
        console.print('[red]Hub directory names must be unique within a deployment so their access domains cannot overlap.[/red]')
        raise typer.Exit(2)
    for h in hub_dirs:
        if not (h / "AGENTS.md").is_file():
            console.print(f"[red]Not a hub (no AGENTS.md):[/red] {h}")
            raise typer.Exit(2)

    # Deployment layer: the gateway's own environment (2b), overridden by the
    # deployment secret named by AWS_SECRET_NAME there (2c). A hub .env never
    # names the deployment secret.
    from functools import partial

    from . import config_secrets

    if df is not None:
        # The deployment's own .env (secrets, sign-in clients, switches) wins
        # over the process environment, as a hub's .env does for the hub.
        root_env = df.root / ".env"
        if root_env.is_file():
            from dotenv import load_dotenv

            load_dotenv(root_env, override=True)
        values, problems = deployfile.environment(df)
        if problems:
            for problem in problems:
                console.print(f"[red]{escape(problem)}[/red]")
            raise typer.Exit(2)
        os.environ.update(values)
        os.environ["HUBZOID_DEPLOYMENT_FILE"] = str(df.path)  # where every bridge reads brand and chat
        public_url = public_url or df.public_url
        name = df.name or name

    process_env = os.environ.copy()
    dep_secret: dict | None = None
    dep_values: dict[str, str] = {}
    secret_name = (process_env.get("AWS_SECRET_NAME") or "").strip()
    if secret_name:
        region = config_secrets.region_for(secret_name, process_env)
        try:
            dep_values = config_secrets.load_secret(secret_name, region=region, layer=config_secrets.DEPLOYMENT)
        except config_secrets.SecretFetchError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        os.environ.update(dep_values)
        dep_secret = {"name": secret_name, "region": region}
        console.print(f"[cyan]→ secrets[/cyan]  deployment secret {secret_name}: {len(dep_values)} key(s)")
        gateway_only = sorted(k for k in dep_values if not config_secrets.bridge_deployment_key(k))
        if gateway_only:
            console.print(f"[yellow]→ secrets[/yellow]  kept from bridges (gateway only): "
                          f"{', '.join(gateway_only)}")
    for h in hub_dirs:
        if gateway_lib._own_env_value(h, "AWS_SECRET_NAME"):
            console.print(f"[yellow]→ secrets[/yellow]  AWS_SECRET_NAME in {h.name}/.env is ignored: the "
                          "deployment secret is named in the gateway's environment. Use "
                          "HUBZOID_HUB_SECRET_NAME for a hub secret.")

    deployment_env = os.environ.copy()
    try:
        # Files only: the gateway never fetches or holds a hub's secrets. Each
        # bridge fetches its own hub and restricted secrets.
        gp = gateway_lib.plan(hub_dirs, load=partial(settingslib.load, secrets=False))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    dev_keys = [b.hub_dir.name for b in gp.backends if b.api_key == "dev"]
    if dev_keys:
        console.print(f"[yellow]→ bridge keys[/yellow]  {', '.join(dev_keys)} use the public bridge key 'dev'. "
                      "Bridges listen on this machine only and never carry identity, so this matters only if "
                      "other people or programs here are not trusted: then set a random BRIDGE_API_KEYS in "
                      "each hub's .env.")

    # All bridges use deployment-wide storage. plan() loads
    # individual hub .env files; the last hub must not redirect the shared DB.
    for key in ("DATABASE_URL", "DATABASE_SCHEMA"):
        if key in deployment_env:
            os.environ[key] = deployment_env[key]
        else:
            os.environ.pop(key, None)

    # Sign-in settings kept in hub .env files still apply to the deployment
    # when the gateway's environment does not set them (0.9.x compat).
    inherited, conflicts = gateway_lib.inherited_deployment_env(hub_dirs, deployment_env)
    os.environ.update(inherited)
    if inherited:
        console.print(f"[yellow]→ settings[/yellow]  from hub .env files: {', '.join(sorted(inherited))}. "
                      "Set these in the gateway's environment instead.")
    for key, hub_names in sorted(conflicts.items()):
        console.print(f"[yellow]→ settings[/yellow]  hubs disagree on {key} ({', '.join(hub_names)}); "
                      "using the last hub listed.")

    ui_port = port or int(os.environ.get("PORT", "3080"))
    pub = (public_url or os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    gw_data = (data_dir or _deployment_state_dir(df)).resolve()
    log_level = os.environ.get("HUB_LOG_LEVEL", "info")
    # Only inspect ports the gateway owns. Independently managed bridges are
    # verified by their health identity below, never stopped or taken over.
    owned_ports = [(host, ui_port, "Public", "--port")]
    owned_ports += [("127.0.0.1", b.bridge_port, "Bridge", "BRIDGE_PORT") for b in gp.backends]
    if len({p for _, p, _, _ in owned_ports}) != len(owned_ports):
        console.print("[red]Gateway and bridge ports must be distinct.[/red]")
        raise typer.Exit(2)
    for bind, listen_port, label, option in owned_ports:
        _ensure_port_available(bind, listen_port, label, option)
    # One shared operational DB for the whole gateway (access grants, per-hub
    # authority markers, identities, audit, workflow catalog): a local URL passed
    # to every bridge, so an org grant in hub A is visible in hub B and one bridge
    # can serve the org-wide portal. DBOS system tables stay per-bridge
    # (db.dbos_url). An operator's HUBZOID_OPERATIONAL_DB / DATABASE_URL wins.
    _op_override = deployment_env.get("HUBZOID_OPERATIONAL_DB") or deployment_env.get("DATABASE_URL")
    if not _op_override:
        gw_data.mkdir(parents=True, exist_ok=True)
    shared_op_url = _op_override or f"sqlite:///{gw_data / 'hubzoid-operational.db'}"

    _migrate_first(shared_op_url)
    _gateway_web_app(
        gp=gp, hub_dirs=hub_dirs, deployment_env=deployment_env, process_env=process_env,
        dep_secret=dep_secret, dep_values=dep_values, conflicts=conflicts, host=host,
        ui_port=ui_port, pub=pub, gw_data=gw_data, log_level=log_level,
        shared_op_url=shared_op_url, name=name, launch_bridges=True)


def _migrate_first(url: str) -> None:
    """Bring the shared store to the current schema once, here, before any hub
    starts: a hub process never upgrades a store others are still using."""
    from . import db as dblib
    from . import migrations

    try:
        migrations.upgrade(dblib._engine_for_url(url), "operational")
    except Exception as exc:  # noqa: BLE001 - said plainly, then stop
        console.print(f"[red]The database could not be brought up to date:[/red] {escape(str(exc))}")
        raise typer.Exit(2)


def _deployment_state_dir(df) -> Path:
    """Where a deployment keeps its state by default: `.hubzoid/` next to its
    hubzoid.yaml, or `./.hubzoid-gateway` for hubs named on the command line.
    A deployment that ran as a gateway keeps that folder: nothing moves."""
    if df is None:
        return Path.cwd() / ".hubzoid-gateway"
    old = df.root / ".hubzoid-gateway"
    new = df.root / ".hubzoid"
    if (old / "deployment.json").is_file() and not (new / "deployment.json").is_file():
        return old
    return new


def _gateway_mode_conflicts(hub_dirs: list[Path], *, auth_on: bool, env, conflicts: dict) -> list[str]:
    """Settings that would make the gateway, or one bridge of it, behave
    differently from the rest: Open WebUI settings that decide sign-in or the
    interface (``appmode.retired_problems``), and hub settings that turn
    sign-in off or bring their own deployment key. Each bridge loads its own
    hub .env over the gateway's environment, so one would split the deployment
    (one bridge without sign-in behind a public edge serves everyone as the
    local owner). Refused, never guessed."""
    from . import appmode
    from . import gateway as gateway_lib

    problems: list[str] = list(appmode.retired_problems(env=env))
    for h in hub_dirs:
        own = gateway_lib._own_env(h)
        problems += [f"{h.name}/.env: {problem}" for problem in appmode.retired_problems(env=own)]
        hub_auth = (own.get("HUBZOID_AUTH") or "").strip()
        if hub_auth and settingslib.truthy(hub_auth) != auth_on:
            problems.append(
                f"{h.name}/.env sets HUBZOID_AUTH={hub_auth}, but the gateway runs with sign-in "
                f"{'on' if auth_on else 'off'}. Set sign-in once, in the gateway's environment.")
        key = (own.get("HUBZOID_SECRET_KEY") or "").strip()
        if key and key != (env.get("HUBZOID_SECRET_KEY") or "").strip():
            problems.append(
                f"{h.name}/.env sets its own HUBZOID_SECRET_KEY. Every bridge of a gateway shares one "
                "deployment key: set it in the gateway's environment (or deployment secret) only.")
    return problems


def _deployment_origins(pub: str, hub_dirs=()) -> list[str]:
    """Every browser origin of the deployment: the public URL (`--public-url`
    or the environment's), HUBZOID_ALLOWED_ORIGINS, then the addresses each
    hub's own .env gives (its public URL and allowed origins). Bridges read the
    list back, so the first hub's bridge, which serves every agent's sign-in
    pages, accepts each address people use."""
    from . import appmode
    from . import gateway as gateway_lib

    env = dict(os.environ)
    if pub:
        env["HUBZOID_PUBLIC_URL"] = pub
    out = appmode.allowed_origins(env)
    for h in hub_dirs:
        out += [o for o in appmode.allowed_origins(gateway_lib._own_env(h)) if o not in out]
    return out


def _gateway_upgrade_guard(gw_data: Path, prior: dict, operational_url: str, *, auth_on: bool) -> None:
    """A gateway that ran Open WebUI before keeps its people and chats there
    until they are moved.

    A manifest that records Open WebUI mode (1.1 to 1.3 with HUBZOID_UI=openwebui)
    starts only after ``hubzoid migrate openwebui --apply`` has run against this
    operational database: accounts made by hand are not a move. A gateway with
    no recorded mode (1.0.x) or one that already ran the web app, with Open
    WebUI data left over, starts once someone has a Hubzoid account (or after
    the move); with sign-in on and nobody to sign in, it stops with
    instructions, and with sign-in off a notice says old chats can be imported.
    The local owner of sign-in-off mode (admin@localhost) does not count. A
    start keeps the manifest's record of Open WebUI's database (see
    _gateway_web_app), so a database server recorded there still counts."""
    server_db = str(prior.get("owui_database_url") or "")
    recorded = str(prior.get("ui_mode") or "").strip().lower()
    had_owui = (gw_data / "webui.db").exists() or (
        bool(prior) and recorded != "hubzoid" and bool(prior.get("owui_url"))) or (
        bool(server_db) and not server_db.startswith("sqlite")) or (recorded not in ("", "hubzoid"))
    if not had_owui:
        return
    from sqlalchemy import text

    from . import db as dblib
    from . import migrations, upgrade
    from .migrate_openwebui import MARKER_PREFIX

    try:
        engine = dblib._engine_for_url(operational_url)
        migrations.upgrade(engine, "operational")
        with engine.connect() as conn:
            accounts = upgrade.people(conn)
            migrated = bool(conn.execute(text("SELECT COUNT(*) FROM hz_meta WHERE k LIKE :p"),
                                         {"p": MARKER_PREFIX + "%"}).scalar())
    except Exception as exc:  # noqa: BLE001 — fail closed: never start blind
        console.print(f"[red]Could not read the Hubzoid accounts ({type(exc).__name__}). "
                      "Check the operational database, then start the gateway again.[/red]")
        raise typer.Exit(1)
    if migrated or (recorded in ("", "hubzoid") and accounts):
        return
    if auth_on or recorded not in ("", "hubzoid"):
        why = ("This gateway ran Open WebUI, and its people and chats have not been moved yet."
               if recorded not in ("", "hubzoid") or accounts else
               "This gateway ran Open WebUI before, and no one has a Hubzoid account yet, "
               "so nobody could sign in.")
        console.print(
            f"[red]{why}[/red]\n"
            "  Move them first:  hubzoid migrate openwebui --apply   "
            "(see hubzoid migrate --help and docs/UPGRADING.md)\n"
            "  Or keep Open WebUI as the chat app: stay on Hubzoid 1.x (pip install \"hubzoid<2\")")
        raise typer.Exit(1)
    console.print("[yellow]→ upgrade[/yellow]  Open WebUI data found here. Old chats can be "
                  "imported into the Hubzoid web app with `hubzoid migrate openwebui`.")


def _wait_any(procs: list, *, interval: float = 0.5, supervisor=None) -> None:
    """With a supervisor (`hubzoid run`), watch and restart its units until
    told to stop. Without one (a hub's own process under a supervisor), a
    child exiting ends this process, so the supervisor restarts the hub as a
    whole.

    SIGINT/SIGTERM use the shutdown handler and return success separately.
    Even a child exiting zero must wake restart-on-failure supervisors.
    """
    if supervisor is not None:
        supervisor.serve(interval=interval)
        return
    while procs:
        for p in procs:
            code = p.poll()
            if code is not None:
                console.print(f"[red]A service exited unexpectedly (status {code}).[/red]")
                raise typer.Exit(1)
        time.sleep(interval)


# How long a web app gateway waits for each bridge (imports, migrations and the
# workflow engine; slower on a busy host).
_GATEWAY_BRIDGE_TIMEOUT = 180.0


def _gateway_bridge_timeout() -> float:
    """HUBZOID_GATEWAY_BRIDGE_TIMEOUT (seconds), else the default."""
    raw = (os.environ.get("HUBZOID_GATEWAY_BRIDGE_TIMEOUT") or "").strip()
    try:
        return max(10.0, float(raw)) if raw else _GATEWAY_BRIDGE_TIMEOUT
    except ValueError:
        console.print(f"[yellow]HUBZOID_GATEWAY_BRIDGE_TIMEOUT={escape(raw)} is not a number of seconds; "
                      f"using {_GATEWAY_BRIDGE_TIMEOUT:.0f}.[/yellow]")
        return _GATEWAY_BRIDGE_TIMEOUT


def _health_of(url: str) -> dict | None:
    """What a bridge's health check says right now, or None when it doesn't answer."""
    import httpx

    try:
        r = httpx.get(url, timeout=3.0)
        body = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else {}


def _stop_groups(procs, *, timeout: float = 15.0) -> None:
    """Stop child services and everything they started. Each was started in its
    own session (process group), so a bridge's server process stops with the
    `hubzoid run` that supervises it, even if that one is killed mid-start."""
    if os.name == "nt":
        from ._process_tree import stop_windows_tree
        active = [p for p in procs if p is not None and p.poll() is None]
        for p in active:
            stop_windows_tree(p)
        deadline = time.monotonic() + max(timeout, 1)
        for p in active:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logging.getLogger(__name__).warning(
                    "Process %s did not exit after forced shutdown", p.pid)
        return
    killpg = getattr(os, "killpg", None)

    def signal_group(p, sig) -> None:
        # killpg(1) means kill(-1): every process this user owns. Only a real
        # child's own group is ever signalled, never 0, 1 or a stand-in's id.
        pid = getattr(p, "pid", None)
        try:
            if killpg is not None and type(pid) is int and pid > 1:
                killpg(pid, sig)
            elif sig == signal.SIGTERM:
                p.terminate()
            else:
                p.kill()
        except (ProcessLookupError, PermissionError):
            pass

    procs = [p for p in procs if p is not None]
    active = [p for p in procs if p.poll() is None]
    for p in active:
        signal_group(p, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    for p in active:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            signal_group(p, signal.SIGKILL)
            p.wait()
    # Anything a supervisor left behind in its group.
    for p in procs:
        signal_group(p, signal.SIGKILL)


def _gateway_web_app(*, gp, hub_dirs, deployment_env, process_env, dep_secret, dep_values,
                     conflicts, host, ui_port, pub, gw_data, log_level, shared_op_url, name,
                     launch_bridges) -> None:
    """`hubzoid gateway`: N bridges and one edge serving the Hubzoid web app.

    The manifest records the mode, sign-in and allowed origins (bridges started
    elsewhere follow it), the deployment key lives next to it (hubzoid.secretbox),
    the edge's default upstream is the first bridge (the others are fallbacks:
    they share the database) and /b/<slug>/{api,artifacts,mcp,branding} reach
    that hub's bridge."""
    from . import appmode, deployment, secretbox

    auth_on = appmode.auth_enabled(env=os.environ)
    problems = _gateway_mode_conflicts(hub_dirs, auth_on=auth_on, env=os.environ, conflicts=conflicts)
    if problems:
        for problem in problems:
            console.print(f"[red]{problem}[/red]")
        raise typer.Exit(2)
    if (not auth_on and not appmode.is_loopback_host(host)
            and not settingslib.truthy(os.environ.get("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK"))):
        console.print(
            f"[red]Sign-in is off, so everyone who can reach {host}:{ui_port} would use every "
            "agent as the local owner.[/red] Turn sign-in on (HUBZOID_AUTH=true), bind to "
            "127.0.0.1, or set HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true to accept that.")
        raise typer.Exit(2)
    if len(hub_dirs) > 1 and deployment_env.get('HUBZOID_DBOS_DB', '').startswith('sqlite'):
        console.print('[red]A multi-hub gateway requires separate SQLite DBOS files. Unset HUBZOID_DBOS_DB or use PostgreSQL.[/red]')
        raise typer.Exit(2)
    if os.environ.get("HUBZOID_DISABLE_EDGE", "").lower() in ("1", "true", "yes"):
        console.print("[yellow]→ edge  [/yellow]  HUBZOID_DISABLE_EDGE is ignored: the edge is the "
                      "gateway's only public door in the Hubzoid web app.")

    try:
        prior = json.loads((gw_data / "deployment.json").read_text())
    except (OSError, ValueError):
        prior = {}
    _gateway_upgrade_guard(gw_data, prior, shared_op_url, auth_on=auth_on)

    # The page chrome is organization-level: HUBZOID_GATEWAY_BRANDING (a hub
    # slug or a path), else <data-dir>/branding when populated, else the first
    # hub's branding/. Recorded so every bridge serves the same folder.
    brand_src = gp.branding_source(gw_data, os.environ.get("HUBZOID_GATEWAY_BRANDING"))
    gw_data.mkdir(parents=True, exist_ok=True)
    owner = next((os.environ.get(k, "").strip() for k in
                  ("HUBZOID_ADMIN_EMAIL",)
                  if os.environ.get(k, "").strip()), None)
    deployment.save(
        gw_data / "deployment.json",
        hubs=[dict(key=b.hub_dir.name.lower(), name=b.display_name or b.slug,
                   path=str(b.hub_dir), model_id=b.model_label, slug=b.slug,
                   dbos_url=deployment_env.get('HUBZOID_DBOS_DB') or
                       (deployment_env.get('DATABASE_URL') if deployment_env.get('DATABASE_URL', '').startswith('postgres')
                        else f"sqlite:///{datadir.state(b.hub_dir) / 'dbos.db'}")) for b in gp.backends],
        operational_url=shared_op_url,
        # A gateway that ran Open WebUI on 1.x keeps where its database is:
        # `hubzoid migrate openwebui` and the upgrade guard find the people and
        # chats still to move there.
        owui_db=str(prior.get("owui_db") or ""),
        owui_database_url=prior.get("owui_database_url") or None,
        owui_database_schema=prior.get("owui_database_schema") or None,
        deployment_secret=dep_secret,
        owner=owner,
        public_url=pub or None,
        workflow_user=os.environ.get("HUBZOID_WORKFLOW_USER"),
        ui_mode=appmode.UI_HUBZOID, auth=auth_on,
        allowed_origins=_deployment_origins(pub, [b.hub_dir for b in gp.backends]),
        name=name, branding_dir=str(brand_src / "branding"))
    console.print(f"[cyan]→ web app[/cyan]  Hubzoid web app, sign-in {'on' if auth_on else 'off'}; "
                  f"branding from {brand_src / 'branding'}")

    # One deployment key for every process, next to the manifest (unless
    # HUBZOID_SECRET_KEY provides it). Created now, before the bridges race to.
    try:
        secretbox.keys(gp.backends[0].hub_dir)
        where = "HUBZOID_SECRET_KEY" if (os.environ.get("HUBZOID_SECRET_KEY") or "").strip() \
            else str(secretbox.key_path(gp.backends[0].hub_dir))
        console.print(f"[cyan]→ key   [/cyan]  deployment key {secretbox.fingerprint(gp.backends[0].hub_dir)} "
                      f"({where}); back it up with the data")
    except secretbox.SecretKeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    procs: list[subprocess.Popen] = []
    previous = _install_shutdown_handlers()
    bridges = [f"http://127.0.0.1:{b.bridge_port}" for b in gp.backends]
    try:
        _gateway_web_app_serve(gp=gp, procs=procs, bridges=bridges, process_env=process_env,
                               dep_secret=dep_secret, dep_values=dep_values, auth_on=auth_on,
                               host=host, ui_port=ui_port, pub=pub, gw_data=gw_data,
                               log_level=log_level, shared_op_url=shared_op_url,
                               launch_bridges=launch_bridges)
    finally:
        try:
            _stop_groups(reversed(procs))
        finally:
            _restore_shutdown_handlers(previous)


def _gateway_web_app_serve(*, gp, procs, bridges, process_env, dep_secret, dep_values, auth_on,
                           host, ui_port, pub, gw_data, log_level, shared_op_url,
                           launch_bridges) -> None:
    """Start the bridges and the edge under the supervisor (their processes are
    recorded in `procs`, which the caller stops), then supervise them: a hub
    that exits is started again on its own, and nothing ends the deployment
    but a stop."""
    from . import appmode, config_secrets
    from . import supervisor as sup_lib

    key = _deployment_bridge_key(gw_data)
    units = []
    for b in gp.backends:
        bridge_env = os.environ.copy()
        if dep_secret:
            # A bridge takes only BRIDGE_DEPLOYMENT_KEYS from the deployment secret and never
            # fetches it again.
            for k in dep_values:
                if config_secrets.bridge_deployment_key(k):
                    continue
                if k in process_env:
                    bridge_env[k] = process_env[k]
                else:
                    bridge_env.pop(k, None)
            bridge_env[config_secrets.INHERITED_MARKER] = "1"
        if pub:
            bridge_env["HUBZOID_PUBLIC_URL"] = gp.public_url_for(pub, b)
        bridge_env["MCP_SERVER"] = "true" if b.mcp else "false"
        bridge_env["MCP_PUBLIC_URL"] = b.mcp_public_url
        bridge_env["HUBZOID_GATEWAY"] = "1"
        bridge_env["HUBZOID_SUPERVISED"] = "1"
        bridge_env["HUBZOID_OPERATIONAL_DB"] = shared_op_url
        # One mode and one sign-in setting for the whole deployment
        # (_gateway_mode_conflicts refused hubs that would differ).
        bridge_env["HUBZOID_UI"] = appmode.UI_HUBZOID
        bridge_env["HUBZOID_AUTH"] = "true" if auth_on else "false"
        # Bridges bind loopback only; the edge is the public door.
        bridge_env["HUBZOID_HOST"] = "127.0.0.1"
        unit_key = b.api_key
        if b.api_key == "dev" and key:
            # No bridge key in the hub's .env: the deployment's own (U9).
            bridge_env["BRIDGE_API_KEYS"] = unit_key = key
        cmd = [sys.executable, "-m", "hubzoid", "run", str(b.hub_dir),
               "--no-ui", "--bridge-port", str(b.bridge_port)]
        specs = [sup_lib.Spec(cmd, bridge_env)] + _channel_specs(
            b.hub_dir, bridge_env, inbound_port=b.inbound_port if b.inbound else None)
        units.append(sup_lib.Unit(
            name=b.hub_dir.name.lower(), slug=b.slug, hub_dir=b.hub_dir, specs=specs,
            health_url=f"http://127.0.0.1:{b.bridge_port}/healthz",
            identity=dict(expected_hub=b.hub_dir.name, expected_model=b.model_label),
            bridge_url=f"http://127.0.0.1:{b.bridge_port}", key=unit_key))

    edge_env = os.environ.copy()
    edge_env["HUBZOID_UI"] = appmode.UI_HUBZOID
    edge_env["HUBZOID_EDGE_DEFAULT"] = bridges[0]
    edge_env["HUBZOID_EDGE_DEFAULT_FALLBACKS"] = json.dumps(bridges[1:])
    edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env, pub)
    edge_env["HUBZOID_EDGE_WORKFLOW_HUBS"] = json.dumps([str(b.hub_dir) for b in gp.backends])
    edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(gp.edge_routes())
    edge_env["HUBZOID_EDGE_TABLE"] = str(gw_data / "routes.json")
    edge_env["HUBZOID_DEPLOYMENT"] = str(gw_data / "deployment.json")
    edge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.edge:_factory", "--factory",
        "--host", host, "--port", str(ui_port),
        "--log-level", log_level,
    ]
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    edge = sup_lib.Unit(name="edge", kind="edge", specs=[sup_lib.Spec(edge_cmd, edge_env, role="edge")],
                        health_url=f"http://{probe_host}:{ui_port}/healthz")
    sup = _supervisor(units + [edge], gw_data, table={
        "default": bridges[0], "default_fallbacks": bridges[1:], "routes": gp.edge_routes()})
    from . import deployfile

    owner = deployfile.owning(gp.backends[0].hub_dir) if gp.backends else None
    if owner is not None and owner.parent / ".hubzoid" != gw_data:
        sup.pointers.append(owner.parent / ".hubzoid")  # `hubzoid status` from the deployment folder

    started = sup.start_all(units)
    procs.extend(p for u in units for p in u.procs)
    ready = [u.slug for u in units if started.get(u.name)]
    console.print(f"[green]→ bridges[/green]  {len(ready)} of {len(units)} ready: {', '.join(ready) or 'none'}")
    for u in units:
        if not started.get(u.name):
            console.print(f"[red]→ {u.slug}[/red]  {escape(u.error or u.last_exit)}. The other agents "
                          f"run; see `hubzoid logs {u.name}`.")
    display_url = f"http://{host}:{ui_port}"
    if not sup.start(edge).get("ok"):
        console.print(f"[red]→ gateway[/red]  failed to start. Check logs. URL: {display_url}")
        raise typer.Exit(1)
    procs.extend(edge.procs)
    console.print(f"[green]→ gateway[/green]  ready    {display_url}")
    _wait_any(procs, supervisor=sup)


def _supervisor(units, state_dir: Path, *, table: dict):
    """The supervisor for these units, holding the deployment's lock."""
    from . import supervisor as sup_lib

    def probe(unit) -> bool | str:
        if _wait_for(unit.health_url, timeout=2.0, **unit.identity):
            return True
        return sup_lib._probe(unit)   # still starting (False), or why it answers as another hub

    def say(text: str) -> None:
        console.print(escape(text))

    try:
        concurrency = int(os.environ.get("HUBZOID_START_CONCURRENCY") or 3)
    except ValueError:
        concurrency = 3
    sup = sup_lib.Supervisor(units, state_dir=state_dir, table=table, probe=probe, say=say,
                             validate=_validate_hub_unit, start_concurrency=concurrency,
                             ready_wait=_gateway_bridge_timeout())
    try:
        sup.lock()
    except RuntimeError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    return sup


def _deployment_bridge_key(state_dir: Path) -> str:
    """The deployment's own bridge key, made once and kept with its state
    (mode 0600), for hubs whose .env sets none."""
    import secrets as _secrets

    path = Path(state_dir) / "bridge.key"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except OSError:
        pass
    value = "hzb_" + _secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
    except OSError:
        return ""
    return value


def _channel_specs(hub_dir: Path, env: dict, *, inbound_port: int | None = None) -> list:
    """The hub's 1.x channel processes, from its channels/ files
    (hubzoid.channels.legacy): the Slack app and the inbound server. They run
    and restart with their hub. Problems (a missing secret) are printed and
    leave that channel off."""
    from . import gateway as gateway_lib
    from . import supervisor as sup_lib
    from .channels import legacy

    plan = legacy.plan(hub_dir, {**os.environ, **gateway_lib._own_env(hub_dir)})
    for note in plan.notes:
        console.print(f"[yellow]→ channels[/yellow] {escape(note)}")
    for problem in plan.problems:
        console.print(f"[yellow]→ channels[/yellow] {escape(problem)}")
    specs = []
    if plan.slack:
        specs.append(sup_lib.Spec([sys.executable, "-m", "hubzoid", "slack", "run", str(hub_dir)], dict(env),
                                  role="channel"))
        console.print(f"[cyan]→ slack [/cyan]  {hub_dir.name}: Slack app (Socket Mode)")
    if plan.inbound:
        inbound_env = dict(env)
        inbound_env["HUBZOID_INBOUND_SURFACES"] = ",".join(plan.inbound)
        if inbound_port:
            inbound_env["HUBZOID_INBOUND_PORT"] = str(inbound_port)
        specs.append(sup_lib.Spec([sys.executable, "-m", "hubzoid", "inbound", "run", str(hub_dir)], inbound_env,
                                  role="channel"))
        console.print(f"[cyan]→ inbound[/cyan] {hub_dir.name}: {'+'.join(plan.inbound)} at /webhooks/<hub>")
    return specs


def _validate_hub_unit(unit) -> list[str]:
    """`hubzoid reload`'s check before restarting a hub: load its folder the
    way its bridge will, in a separate process (tools are code). Problems are
    returned as lines; none means go ahead."""
    if unit.hub_dir is None:
        return []
    try:
        out = subprocess.run([sys.executable, "-m", "hubzoid", "check-hub", str(unit.hub_dir)],
                             capture_output=True, text=True, timeout=120,
                             env={**os.environ, **(unit.specs[0].env if unit.specs else {})})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"could not check the hub: {exc}"]
    if out.returncode == 0:
        return []
    return [line for line in (out.stdout + out.stderr).splitlines() if line.strip()][-20:] or \
        [f"check failed (status {out.returncode})"]


# ---------------------------------------------------------------------------
# control: status, restart, stop, start, reload, logs (hubzoid.supervisor)
# ---------------------------------------------------------------------------
_DATA_DIR_HELP = "The deployment's state folder, when it isn't found from this folder."


def _find_state_dir(data_dir: Path | None) -> Path:
    """The running deployment's state folder: --data-dir, else the one for
    hubzoid.yaml or the hub in this folder (or a parent)."""
    from . import deployfile
    from . import supervisor as sup_lib

    if data_dir is not None:
        return data_dir.resolve()
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        candidates = []
        if deployfile.find(folder):
            candidates += [folder / ".hubzoid", folder / ".hubzoid-gateway"]
        candidates += [folder / ".hubzoid-gateway"]
        if (folder / "AGENTS.md").is_file():
            candidates.append(datadir.state(folder))
        for c in candidates:
            if sup_lib.control_file(c).is_file():
                return c
    raise_dir = here / ".hubzoid"
    return raise_dir


def _control(op: str, data_dir: Path | None, **payload) -> dict:
    from . import supervisor as sup_lib

    state = _find_state_dir(data_dir)
    try:
        return sup_lib.request(state, {"op": op, **payload})
    except ConnectionError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)


def _report(out: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
    elif not out.get("ok") and out.get("error"):
        console.print(f"[red]{escape(out['error'])}[/red]")
    else:
        for r in out.get("results", []):
            if r.get("problems"):
                console.print(f"[red]{escape(r['hub'])}: not restarted, the folder has problems:[/red]")
                for line in r["problems"]:
                    console.print(f"  {escape(line)}")
            else:
                colour = "green" if r.get("ok") else "red"
                extra = f" ({r['error']})" if r.get("error") else ""
                console.print(f"[{colour}]{escape(r['hub'])}: {escape(str(r.get('state')))}{escape(extra)}[/{colour}]")
    if not out.get("ok"):
        raise typer.Exit(1)


@app.command("status", rich_help_panel="Run")
def status_cmd(
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Each hub of the running deployment: its state, restarts, last exit,
    and whether it needs a reload."""
    out = _control("status", data_dir)
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    for u in out.get("units", []):
        since = time.strftime("%H:%M:%S", time.localtime(u["since"])) if u.get("since") else "-"
        line = f"{u['hub']:<24} {u['state']:<11} since {since}  restarts {u['restarts']}"
        if u.get("retry_in") is not None:
            line += f"  (again in {u['retry_in']:.0f} s)"
        if u.get("error"):
            line += f"  {u['error']}"
        colour = {"ready": "green", "failed": "red", "restarting": "yellow", "draining": "yellow"}.get(u["state"], "")
        console.print(f"[{colour}]{escape(line)}[/{colour}]" if colour else escape(line))
        for why in u.get("reload_needed") or []:
            console.print(f"  [yellow]reload needed:[/yellow] {escape(why)}  (`hubzoid reload {u['hub']}`)")


@app.command("restart", rich_help_panel="Run")
def restart_cmd(
    hubs: list[str] = typer.Argument(..., help="Hubs to restart, by folder name or slug."),
    grace: float = typer.Option(None, "--grace", help="Seconds running work may take to finish. Default 120."),
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Restart hubs one after the other, each drained first. The others keep
    running."""
    _report(_control("restart", data_dir, hubs=hubs, grace=grace), as_json)


@app.command("reload", rich_help_panel="Run")
def reload_cmd(
    hubs: list[str] = typer.Argument(..., help="Hubs to reload, by folder name or slug."),
    grace: float = typer.Option(None, "--grace", help="Seconds running work may take to finish. Default 120."),
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Check a hub's folder, then restart it (drained) so every change takes
    effect. A folder with problems changes nothing."""
    _report(_control("reload", data_dir, hubs=hubs, grace=grace), as_json)


@app.command("stop", rich_help_panel="Run")
def stop_cmd(
    hubs: list[str] = typer.Argument(..., help="Hubs to stop."),
    grace: float = typer.Option(None, "--grace", help="Seconds running work may take to finish. Default 120."),
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Stop hubs (drained) until `hubzoid start`. The others keep running."""
    _report(_control("stop", data_dir, hubs=hubs, grace=grace), as_json)


@app.command("start", rich_help_panel="Run")
def start_cmd(
    hubs: list[str] = typer.Argument(..., help="Hubs to start."),
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Start stopped or failed hubs again."""
    _report(_control("start", data_dir, hubs=hubs), as_json)


@app.command("logs", rich_help_panel="Run")
def logs_cmd(
    hub: str = typer.Argument(..., help="The hub, or `edge`."),
    lines: int = typer.Option(200, "--lines", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new lines."),
    data_dir: Path = typer.Option(None, "--data-dir", help=_DATA_DIR_HELP),
) -> None:
    """A hub's recent output, from its log file in the data folder."""
    out = _control("logs", data_dir, hub=hub, lines=lines)
    if not out.get("ok"):
        _report(out, False)
    for line in out.get("lines", []):
        typer.echo(line)
    if follow:
        path = Path(out["path"])
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            try:
                while True:
                    line = fh.readline()
                    if line:
                        typer.echo(line.rstrip("\n"))
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                return


@app.command("check-hub", hidden=True)
def check_hub(hub: Path = typer.Argument(..., help="Hub folder.")) -> None:
    """Load a hub's folder the way its bridge will (instructions, skills,
    local tools, channel files). Used by `hubzoid reload`. Exit 1 on problems."""
    hub = hub.resolve()
    problems: list[str] = []
    try:
        settingslib.load(hub, secrets=False)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        problems.append(f".env: {exc}")
    try:
        from .loaders.agents import load_main

        load_main(hub)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"AGENTS.md: {exc}")
    try:
        from .loaders import tools_local

        tools_local.load_all(hub)
        # A broken file only loses its own tools at run time (the loader logs
        # it and keeps going), so the check reads what failed to load.
        problems += [f"tools_local/{Path(f).name}: {why}" for f, why in sorted(tools_local.LOAD_ERRORS.items())]
    except Exception as exc:  # noqa: BLE001
        problems.append(f"tools_local: {type(exc).__name__}: {exc}")
    try:
        from .channels import config as channel_config

        for path in channel_config.files(hub):
            v = channel_config.load_file(hub, path)
            if not v.valid:
                problems += [f"{path.relative_to(hub)}: {e.get('message') or e}" for e in v.errors]
    except Exception as exc:  # noqa: BLE001
        problems.append(f"channels: {exc}")
    for p in problems:
        typer.echo(p)
    if problems:
        raise typer.Exit(1)


@app.command("upgrade", rich_help_panel="Run")
def upgrade_cmd(
    folder: Path = typer.Argument(Path("."), help="The deployment folder: hubzoid.yaml, a hub, or where "
                                                  "`hubzoid gateway` ran."),
    data_dir: Path = typer.Option(None, "--data-dir", help="The state folder (a gateway's --data-dir)."),
    hubs: list[Path] = typer.Option(None, "--hub", help="Hub folders, when there is no hubzoid.yaml or manifest."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would be written; change nothing."),
) -> None:
    """Move a deployment to one run mode, once, offline, after `hubzoid backup`:
    write every setting kept only in the database into its file (hubzoid.yaml,
    branding/, connectors/, .env, channel files), export Workspace drafts, then
    bring the database to the current schema. Never overwrites a file and never
    prints a secret."""
    from . import db as dblib
    from . import deployfile
    from . import one_home

    root = folder.resolve()
    found = deployfile.find(root)
    df = deployfile.load(found) if found else None
    # Which hubs: the deployment file, then --hub, then a gateway's manifest,
    # and only then the folder itself as a hub (a repository of hubs often has
    # an AGENTS.md of its own for coding agents).
    if data_dir is not None:
        state = data_dir.resolve()
    elif df is not None:
        state = _deployment_state_dir(df)
    elif (root / ".hubzoid-gateway" / "deployment.json").is_file():
        state = root / ".hubzoid-gateway"
    else:
        state = root / ".hubzoid"
    manifest_hubs: list[Path] = []
    try:
        manifest = json.loads((state / "deployment.json").read_text())
        manifest_hubs = [Path(h["path"]) for h in manifest.get("hubs", [])]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if df is not None:
        hub_dirs = df.hub_dirs()
    elif hubs:
        hub_dirs = [h.resolve() for h in hubs]
    elif manifest_hubs:
        hub_dirs = manifest_hubs
    elif (root / "AGENTS.md").is_file():
        hub_dirs = [root]
        state = data_dir.resolve() if data_dir is not None else datadir.state(root)
    else:
        hub_dirs = []
    if not hub_dirs:
        console.print("[red]No hubs found.[/red] Run it in the deployment folder, or pass --data-dir or --hub.")
        raise typer.Exit(2)
    from . import supervisor as sup_lib

    probe = sup_lib.Supervisor([], state_dir=state)
    try:
        probe.lock()
    except RuntimeError:
        console.print("[red]This deployment is running.[/red] Stop it first (systemctl stop hubzoid), take "
                      "a backup (hubzoid backup), then run hubzoid upgrade.")
        raise typer.Exit(2)
    try:
        url = dblib.operational_url(hub_dirs[0])
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    console.print(f"[cyan]→ upgrade[/cyan]  {root}  (state {state}, {len(hub_dirs)} hub(s))")
    up = one_home.Upgrade(root, state, hub_dirs, dblib._engine_for_url(url), dry_run=dry_run,
                          say=lambda t: console.print(f"  {escape(t)}"))
    try:
        done = up.run()
    except Exception as exc:  # noqa: BLE001 - said plainly
        console.print(f"[red]The upgrade stopped:[/red] {escape(str(exc))}")
        raise typer.Exit(1)
    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] {len(done)} change(s) listed, none made.")
    else:
        console.print(f"[green]Done.[/green] Review the files (git diff), then start with `hubzoid run`"
                      f"{' --data-dir ' + str(state) if data_dir else ''}.")


service_app = typer.Typer(no_args_is_help=True, help="Run the deployment as a system service.")
app.add_typer(service_app, name="service", rich_help_panel="Run")


@service_app.command("install")
def service_install(
    folder: Path = typer.Argument(Path("."), help="The deployment folder (hubzoid.yaml, or a hub)."),
    port: int = typer.Option(3080, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    data_dir: Path = typer.Option(None, "--data-dir", help="State folder, if not the default."),
    user: str = typer.Option("", "--user", help="The user the service runs as (default: root)."),
    write: bool = typer.Option(False, "--write", help="Write /etc/systemd/system/hubzoid.service."),
) -> None:
    """One systemd unit for the whole deployment: `hubzoid run`, restarted if
    it stops. Restart single hubs with `hubzoid restart <hub>`."""
    folder = folder.resolve()
    args = ["run", "--port", str(port), "--host", host, "--no-open"]
    if data_dir is not None:
        args += ["--data-dir", str(data_dir.resolve())]
    exe = Path(sys.executable).with_name("hubzoid")
    lines = ["[Unit]", "Description=Hubzoid", "After=network-online.target", "Wants=network-online.target", "",
             "[Service]", f"WorkingDirectory={folder}", f"ExecStart={exe} {' '.join(shlex.quote(a) for a in args)}",
             "Restart=always", "RestartSec=5", "KillMode=mixed", "TimeoutStopSec=180"]
    if user:
        lines.append(f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    unit = "\n".join(lines)
    if not write:
        typer.echo(unit)
        console.print("[dim]Save it as /etc/systemd/system/hubzoid.service (or pass --write), then:[/dim]")
        console.print("  systemctl daemon-reload && systemctl enable --now hubzoid", highlight=False)
        return
    target = Path("/etc/systemd/system/hubzoid.service")
    try:
        target.write_text(unit)
    except OSError as exc:
        console.print(f"[red]Could not write {target}: {exc}. Run as root, or save the unit by hand.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Wrote {target}.[/green] Now: systemctl daemon-reload && systemctl enable --now hubzoid")


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------
class _BackupGroup(TyperGroup):
    """`hubzoid backup [HUB]` takes a backup (the `create` command); `verify` and
    `list` are the group's other commands."""

    def parse_args(self, ctx, args):
        if not args or (args[0] not in self.commands and args[0] not in ("--help", "-h")):
            args = ["create", *args]
        return super().parse_args(ctx, args)


backup_app = typer.Typer(
    cls=_BackupGroup, no_args_is_help=False,
    help="Save a deployment's state to one verified archive: `hubzoid backup [HUB]`. "
         "Also `hubzoid backup verify ARCHIVE` and `hubzoid backup list [HUB]`. Scheduled backups "
         "run weekly by default (HUBZOID_BACKUPS=weekly|daily|off).")
app.add_typer(backup_app, name="backup", rich_help_panel="Commands")

from .deploy.cli import deploy_app  # noqa: E402

app.add_typer(deploy_app, name="deploy", rich_help_panel="Commands")


@backup_app.command("create")
def backup_cmd(
    hub: Path = typer.Argument(Path("."), help="A hub directory. A hub in a gateway backs up the whole gateway."),
    out: Path = typer.Option(None, "--out", "-o", help="Archive path. Default: ./hubzoid-backup-<time>.tar.gz"),
    include_secrets: bool = typer.Option(False, "--include-secrets", help="Also save .env files and signing keys."),
    wait: int = typer.Option(600, "--wait", help="Seconds to wait for running scheduled work. 0 = do not wait."),
) -> None:
    """Save a deployment's state to one archive while it keeps serving chat.

    New scheduled runs are held and running ones finish first. PostgreSQL
    databases are saved with pg_dump. The archive is verified before it counts.
    Hub content (AGENTS.md, skills, knowledge) belongs in git and is not
    included (see docs/BACKUP.md)."""
    from datetime import datetime as _dt

    from . import backup as backup_lib

    out = out or Path.cwd() / f"hubzoid-backup-{_dt.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    try:
        index = backup_lib.backup(hub.resolve(), out, include_secrets=include_secrets,
                                  wait=wait, actor=_operator(), say=console.print)
    except (backup_lib.BackupError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    checked = index["verified"]
    console.print(f"[green]Backup written and verified:[/green] {out.resolve()} "
                  f"({checked['files']} files, {len(index['sqlite'])} databases"
                  + (f", {len(index['postgres'])} PostgreSQL" if index["postgres"] else "") + ")")
    if any(r["kind"] == "ui" for r in index["roots"]):
        console.print("The archive holds user accounts, chats and the chat UI's connection settings. "
                      "Store it like a secret.")
    else:
        # A hub behind a gateway that has not started on this release yet has no
        # pointer to the gateway, so only this hub's state was found.
        console.print("Hubzoid runtime state was saved. Accounts and "
                      "conversations live in the operational database. A hub behind a gateway is covered only after the gateway "
                      "has started on this version. Before that, also archive the gateway's "
                      "--data-dir (see docs/UPGRADING.md). Store the archive like a secret.")
    if not include_secrets:
        console.print("Left out: .env files, deployment encryption keys, signing keys and database "
                      "passwords. Keep protected copies separately (see docs/BACKUP.md).")
    for url in index["not_included"]:
        console.print(f"[yellow]Not included:[/yellow] {url}.")


@backup_app.command("verify")
def backup_verify(
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, help="An archive from hubzoid backup."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Check an archive: every file's checksum, each database's integrity and each PostgreSQL dump.

    Exits 1 when it fails."""
    from . import backup as backup_lib

    result = backup_lib.verify(archive)
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        mark = "[green]✓ Verified[/green]" if result["ok"] else "[red]✗ Not verified[/red]"
        console.print(f"{mark} {archive}: {result['files']} files, {result['databases']} databases"
                      + (f", {result['postgres']} PostgreSQL" if result["postgres"] else ""))
        for line in result["problems"]:
            console.print(f"  [red]✗[/red] {escape(line)}")
        for line in result["warnings"]:
            console.print(f"  [yellow]![/yellow] {escape(line)}")
    if not result["ok"]:
        raise typer.Exit(1)


@backup_app.command("list")
def backup_list(
    hub: Path = typer.Argument(Path("."), help="A hub directory."),
    as_json: bool = typer.Option(False, "--json", help="Print the list as JSON."),
) -> None:
    """The scheduled backups kept for this deployment, newest first."""
    from . import backup_schedule

    hub = hub.resolve()
    items = backup_schedule.archives(hub)
    folder = backup_schedule.folder(hub)
    if as_json:
        print(json.dumps({"folder": str(folder), "schedule": os.environ.get(backup_schedule.SETTING) or "weekly",
                          "archives": items}, indent=2))
        return
    console.print(f"[dim]{folder} · schedule {os.environ.get(backup_schedule.SETTING) or 'weekly'}[/dim]")
    if not items:
        console.print("No scheduled backups yet.")
    for a in items:
        console.print(f"{a['created']}  {a['bytes'] / 1e6:8.1f} MB  {a['path']}")


@app.command("restore")
def restore_cmd(
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, help="An archive from hubzoid backup."),
    move: list[str] = typer.Option([], "--move", help="OLD=NEW: restore paths under OLD to NEW instead. Repeatable."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show where everything would go and stop."),
    rehearse: bool = typer.Option(False, "--rehearse", help="Restore into a scratch folder, upgrade the copies, "
                                                            "report and clean up. Touches nothing else."),
    postgres: bool = typer.Option(False, "--postgres", help="Also load the archive's PostgreSQL dump into the "
                                                            "database named by --postgres-env."),
    postgres_env: str = typer.Option(None, "--postgres-env", help="The environment variable holding that "
                                     "database's URL (default DATABASE_URL). With --rehearse: a server "
                                     "where a temporary database is created and dropped."),
    as_json: bool = typer.Option(False, "--json", help="With --rehearse: print the report as JSON."),
) -> None:
    """Put a backup back. Stop the hub or gateway first.

    Everything returns to its original path unless moved with --move. What is
    at a target now is kept beside it as <name>.pre-restore-<time>. The URL of a
    PostgreSQL database is read from an environment variable, never given on
    the command line."""
    from . import backup as backup_lib

    def _pg_url(name: str | None) -> str | None:
        if not name:
            return None
        value = (os.environ.get(name) or "").strip()
        if not value:
            console.print(f"[red]{name} is not set.[/red]")
            raise typer.Exit(2)
        return value

    if rehearse:
        report = backup_lib.rehearse(archive, postgres_url=_pg_url(postgres_env), say=lambda _m: None)
        if as_json:
            print(json.dumps(report, indent=2))
        else:
            mark = "[green]✓ Rehearsal passed[/green]" if report["ok"] else "[red]✗ Rehearsal failed[/red]"
            console.print(f"{mark} for {archive} ({report.get('seconds', 0)} s). Nothing was changed.")
            for d in report["databases"] + report["postgres"]:
                name = d.get("database") or d.get("dump")
                if d.get("skipped"):
                    console.print(f"  [yellow]![/yellow] {name}: skipped ({d['skipped']}; pass --postgres-env)")
                elif d.get("problems"):
                    console.print(f"  [red]✗[/red] {name}: {escape('; '.join(d['problems']))}")
                else:
                    rows = ", ".join(f"{v} {k.removeprefix('hz_')}" for k, v in (d.get("rows") or {}).items())
                    console.print(f"  [green]✓[/green] {name}" + (" upgraded" if d.get("upgraded") else "")
                                  + (f": {rows}" if rows else ""))
            for line in report["problems"] if not report["databases"] else []:
                console.print(f"  [red]✗[/red] {escape(line)}")
        if not report["ok"]:
            raise typer.Exit(1)
        return

    moves = []
    for m in move:
        old, sep, new = m.partition("=")
        if not sep or not old or not new:
            console.print(f"[red]--move expects OLD=NEW, got {m!r}[/red]")
            raise typer.Exit(2)
        moves.append((old, str(Path(new).expanduser().resolve())))
    try:
        planned = backup_lib.restore_plan(archive, moves)
        if dry_run:
            for root, target in planned:
                console.print(f"{root['path']} -> {target}" + (" (exists; kept aside)" if target.exists() else ""))
            return
        result = backup_lib.restore(archive, moves, say=console.print,
                                    postgres_url=_pg_url(postgres_env or "DATABASE_URL") if postgres else None)
    except backup_lib.BackupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    for aside in result["kept"]:
        console.print(f"Previous state kept at {aside}")
    for url in result["not_included"]:
        console.print(f"[yellow]Not in this archive:[/yellow] {url}.")
    if result.get("postgres") and not result.get("postgres_restored"):
        console.print("[yellow]The archive's PostgreSQL dump was not loaded.[/yellow] Pass --postgres to load it "
                      "into the database named by --postgres-env (default DATABASE_URL).")
    for path in result.get("redacted", []):
        console.print(f"[yellow]Database passwords in {path} were saved as ***.[/yellow] "
                      "`hubzoid gateway` rewrites this file from its environment when it starts, "
                      "so start the gateway before any bridge, or put the passwords back by hand.")
    console.print("[green]Restore complete.[/green] Start the hub or gateway, then run `hubzoid doctor`.")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
@app.command()
def doctor(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Print the checks as JSON (stable check ids)."),
    skip_secret_fetch: bool = typer.Option(
        False, "--skip-secret-fetch",
        help="Do not call AWS Secrets Manager. Named secrets are listed but not read."),
) -> None:
    """Check a hub and its deployment: files, configuration layers and secrets,
    agent build, schedules, schema, auth, exposure, model credentials, backups
    and scheduled work. Read only. Exits 1 when any check fails or a required
    eval is pending."""
    import json as _json

    from . import doctor as doctor_lib

    hub = hub.resolve()
    if not hub.is_dir():
        if as_json:
            print(_json.dumps({"format": doctor_lib.FORMAT, "hub": str(hub), "ok": False, "checks": [
                {"id": "hub.dir", "status": "fail", "summary": "Hub directory not found", "detail": None}]}))
        else:
            console.print(f"[red]Hub directory not found:[/red] {hub}")
        raise typer.Exit(2)
    checks = doctor_lib.run(hub, fetch_secrets=not skip_secret_fetch)
    result = doctor_lib.report(hub, checks)
    if as_json:
        print(_json.dumps(result, indent=2, default=str))
    else:
        marks = {"ok": "[green]✓[/green]", "info": "[dim]·[/dim]", "warn": "[yellow]![/yellow]",
                 "fail": "[red]✗[/red]", "pending": "[cyan]…[/cyan]"}
        for c in checks:
            console.print(f"{marks[c.status]} {c.summary} [dim]({c.id})[/dim]")
            if c.id == "config.layers" and isinstance(c.detail, list):
                # Names and sources only. The report never holds a value.
                for row in c.detail:
                    console.print(f"    [dim]{row['key']}: {row['layer']} ({row['source']})[/dim]")
            elif c.status in ("warn", "fail") and isinstance(c.detail, list):
                for line in c.detail:
                    console.print(f"    [dim]{line}[/dim]")
    if not result["ok"]:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
@app.command()
def audit(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    limit: int = typer.Option(50, "--limit", "-n", help="Show the most recent N decisions."),
    user: str = typer.Option(None, "--user", help="Filter to one user."),
    denied: bool = typer.Option(False, "--denied", help="Show only denied attempts."),
) -> None:
    """Show the access log: who called which restricted tool, allowed or denied.

    Reads the decision log the runtime writes to the operational database for
    every restricted tool call. Open WebUI does not record tool-level access, so
    this is the only place it exists.
    """
    from .access import audit as auditlib

    hub = hub.resolve()
    rows = auditlib.read(hub, limit=limit, user=user, decision=("deny" if denied else None))
    if not rows:
        console.print("[dim]no access decisions logged yet[/dim]")
        return
    for r in rows:
        ok = r.get("decision") == "allow"
        colour = "green" if ok else "red"
        console.print(
            f"[dim]{r.get('ts', '')}[/dim]  "
            f"{(r.get('user') or '?'):22.22}  "
            f"[{colour}]{r.get('decision', '?').upper():5}[/{colour}]  "
            f"{(r.get('tool') or '?'):22.22}  "
            f"[dim]{r.get('surface', '')}·{r.get('reason', '')}[/dim]"
        )
    console.print(f"[dim]{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} shown[/dim]")


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------
# Stable chat id for `hubzoid test --file`. One predictable directory to
# inspect (`<hub>/.hubzoid/chats/cli-test/`) instead of a new random one per
# run, so uploads and artifacts stay where you left them between invocations.
_TEST_CHAT_ID = "cli-test"


def _attachment_blocks(files: list[Path]) -> list[dict]:
    """Encode local files as the data-URL content blocks the bridge parses.

    `_persist_attachments` speaks data URLs because that is what arrives
    over HTTP. Re-encoding bytes we just read costs a base64 round-trip,
    and buys attachment handling identical to real traffic — same
    safe-naming, same sidecars, same notes — with no logic duplicated here.
    """
    blocks: list[dict] = []
    for path in files:
        payload = path.read_bytes()
        mime = uploadslib.guess_mime(path.name)
        b64 = base64.b64encode(payload).decode("ascii")
        blocks.append({
            "type": "file",
            "name": path.name,
            "data": f"data:{mime};base64,{b64}",
        })
    return blocks


def _stage_test_attachments(
    hub: Path,
    chat_id: str,
    files: list[Path],
    prompt: str,
    *,
    max_upload_bytes: int,
) -> str:
    """Stage `files` into the chat's uploads dir; return the annotated prompt.

    Delegates to the bridge's own handler, so the agent sees exactly what it
    would see from Open WebUI or Slack: `[Image: x]` for images (expanded by
    `vision_inject` at model-call time) and a `read_upload('x')` pointer for
    everything else.

    Raises HTTPException(413) if any file exceeds the cap; nothing is written
    in that case — a half-staged set is worse than a refused one.
    """
    if not files:
        return prompt

    from .server import _persist_attachments

    messages = [{"role": "user", "content": _attachment_blocks(files)}]
    notes = _persist_attachments(
        hub, chat_id, messages, max_upload_bytes=max_upload_bytes
    )
    if not notes:
        return prompt
    return "\n\n".join(notes) + "\n\n" + prompt


@app.command("test")
def test_hub(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    prompt: str = typer.Option("Reply with the single word: pong", "--prompt", help="Test prompt to send."),
    file: list[Path] = typer.Option(
        None, "--file", "-f",
        exists=True, dir_okay=False, readable=True,
        help="Attach a local file (repeatable). Staged into the 'cli-test' chat so "
             "the agent can read it with read_upload, or see it directly if it's an image.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the reply, tool calls and errors as JSON."),
) -> None:
    """Send one prompt to the hub's agent and print the response.

    Runs in-process (no bridge / no UI). Backend is picked from MODEL in .env:
    `claude-local` -> Claude Agent SDK; anything else -> OpenAI Agents SDK.

    With `--file`, the run is scoped to the `cli-test` chat so the upload tools
    resolve. Without it the run stays unscoped, exactly as before — that keeps
    `write_artifact` writing to the session output dir and reporting a local
    path, rather than a download URL for a bridge that isn't running.
    """
    import asyncio
    from contextlib import nullcontext

    from fastapi import HTTPException

    hub = hub.resolve()
    files = list(file or [])

    # Stage before building the runtime: an over-cap attachment should fail
    # fast, not after paying for model/MCP init.
    if files:
        try:
            prompt = _stage_test_attachments(
                hub, _TEST_CHAT_ID, files, prompt,
                max_upload_bytes=settingslib.load(hub).max_upload_bytes,
            )
        except HTTPException as exc:
            console.print(f"[red]{exc.detail}[/red]")
            raise typer.Exit(2) from exc

    from . import _request_ctx
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

    # ContextVars set here are visible inside asyncio.run(): the task it
    # creates copies the context at creation time, i.e. inside this block.
    scope = _request_ctx.chat_scope(_TEST_CHAT_ID) if files else nullcontext()
    if as_json:
        from . import run_events

        async def _events() -> dict:
            await rt.aopen()
            out: dict = {"prompt": prompt, "reply": "", "tool_calls": [], "errors": []}
            try:
                stream = run_events.stream_items(rt, prompt)
                try:
                    async for item in stream:
                        if isinstance(item, str):
                            out["reply"] += item
                        elif isinstance(item, run_events.ToolCall):
                            out["tool_calls"].append({"id": item.id, "name": item.name, "ok": None})
                        elif isinstance(item, run_events.ToolResult):
                            for call in out["tool_calls"]:
                                if call["id"] == getattr(item, "id", None):
                                    call["ok"] = getattr(item, "ok", None)
                        elif isinstance(item, run_events.Notice) and item.kind == "error":
                            out["errors"].append(item.text)
                finally:
                    await run_events.aclose(stream)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"{type(exc).__name__}: {exc}")
            finally:
                await rt.aclose()
            return out

        with scope:
            result = asyncio.run(_events())
        print(json.dumps(result, indent=2, default=str))
        if result["errors"]:
            raise typer.Exit(1)
        return
    with scope:
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
    from .slack.adapter import run as run_adapter
    from .slack.env import EnvError

    hub = hub.resolve()
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]No AGENTS.md in {hub}. Run `hubzoid init` first.[/red]")
        raise typer.Exit(2)

    # Trigger .env load so SLACK_* vars are visible to the adapter and
    # settings.load() sees the same picture as `hubzoid run`.
    _load_settings(hub)
    from . import supervisor as sup_lib

    sup_lib.watch_parent()
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
    try:
        unit = systemd_unit_for_hub(hub_dir=hub, python_path=python_path, user=user)
    except ValueError as exc:  # the deployment and the hub disagree on the shared store
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    typer.echo(unit)


inbound_app = typer.Typer(
    help="WhatsApp/Telegram webhook surfaces: run the server or print a systemd unit.",
    no_args_is_help=True,
)


@inbound_app.command("run")
def inbound_run(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Serve the WhatsApp/Telegram webhook app for a hub. Foreground; ^C to stop.

    Requires the hub's bridge to be running (`hubzoid run <hub>`). Reads
    WHATSAPP_* and/or TELEGRAM_* from <hub>/.env and serves whichever are set.
    """
    from .inbound.run import run as run_inbound

    hub = hub.resolve()
    if not (hub / "AGENTS.md").is_file():
        console.print(f"[red]No AGENTS.md in {hub}. Run `hubzoid init` first.[/red]")
        raise typer.Exit(2)
    _load_settings(hub)
    from . import supervisor as sup_lib

    sup_lib.watch_parent()
    raise typer.Exit(run_inbound(hub))


@inbound_app.command("systemd")
def inbound_systemd(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    user: str = typer.Option("hubzoid", "--user", help="Linux user to run the service as."),
    python: Path = typer.Option(
        None, "--python", help="Python interpreter path. Default: detect from current sys.executable."
    ),
) -> None:
    """Print a systemd unit for hubzoid-inbound@<hub>.service to stdout."""
    from .inbound.service import systemd_unit_for_hub

    hub = hub.resolve()
    python_path = python or Path(sys.executable).resolve()
    try:
        unit = systemd_unit_for_hub(hub_dir=hub, python_path=python_path, user=user)
    except ValueError as exc:  # the deployment and the hub disagree on the shared store
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    typer.echo(unit)


app.add_typer(
    inbound_app,
    name="inbound",
    help="WhatsApp/Telegram webhook surfaces: run the server or print a systemd unit.",
)


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
    as_json: bool = typer.Option(False, "--json", help="Print the list as JSON."),
) -> None:
    """List the hub's scheduled tasks, their cadence and next fire time."""
    from datetime import datetime

    from . import scheduling as sch

    hub = hub.resolve()
    tasks, problems = sch.load_tasks(hub)
    state = sch.ScheduleState(hub)
    now = datetime.now()
    from .workflows.observe import catalog
    workflows = catalog(hub)
    if as_json:
        out = {"workflows": workflows, "tasks": [], "problems": [str(p) for p in problems]}
        for t in tasks:
            nxt = None if t.is_webhook or not t.enabled else sch.next_fire_for(t, state, now)
            entry = state.get(t.name)
            out["tasks"].append({"name": t.name, "enabled": t.enabled, "schedule": t.schedule,
                                 "on_webhook": t.on_webhook if t.is_webhook else None,
                                 "next_fire": nxt.isoformat() if nxt else None,
                                 "last_result": entry.get("last_result"), "last_fired": entry.get("last_fired_iso"),
                                 "script": t.is_script, "model": t.model})
        print(json.dumps(out, indent=2, default=str))
        if problems:
            raise typer.Exit(1)
        return
    for w in workflows:
        ra = w["runs_as"]
        if ra["error"]:
            runs_as = f"[red]cannot run:[/red] {ra['error']}"
        elif ra["source"] == "legacy-service":
            runs_as = (f"runs as the legacy service identity {ra['account']} "
                       "(set run_as or HUBZOID_WORKFLOW_USER)")
        else:
            runs_as = f"runs as {ra['account']} ({ra['via']})"
        console.print(f"workflow {w['name']} · {w['state']} · {w['timezone']} · next {w['next_run'] or '—'}"
                      f" · {runs_as}")
        if w['error']:
            console.print(f"[red]{w['error']}[/red]")
    if not tasks and not problems and not workflows:
        console.print(f"no tasks — add schedule/*.md or workflows/<name>/*.py under {hub}")
        return
    for t in tasks:
        nxt = sch.next_fire_for(t, state, now)
        entry = state.get(t.name)
        last = entry.get("last_fired_iso")
        last_s = f"last: {entry.get('last_result', '?')} @ {last}" if last else "never fired"
        flags = []
        if t.is_script:
            flags.append("script")
        if t.model:
            flags.append(f"model: {t.model}")
        if t.commit:
            flags.append(f"commit: {', '.join(t.commit)}" + (" + push" if t.push else ""))
        # Webhook-triggered tasks have no cron/next-fire — they show their trigger
        # and fire when an event lands.
        when = f"on webhook {t.on_webhook}" if t.is_webhook else t.schedule
        if not t.enabled:
            console.print(f"[dim]⏸ {t.name}  ({when})  disabled[/dim]")
            continue
        if t.is_webhook:
            console.print(
                f"[green]●[/green] [bold]{t.name}[/bold]  on webhook '{t.on_webhook}'"
                f"  →  fires on event  ·  {last_s}"
                + (f"  ·  {'; '.join(flags)}" if flags else "")
            )
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


def _cli_envelope(hub: Path, name: str, pairs) -> dict:
    """How a CLI run starts: a team run, recorded as started from the CLI, with
    validated `--input` values. ValueError carries the one-line problem."""
    from .workflows import inputs as inputs_lib
    from .workflows import runtime as _wf

    definition = next((d for d in _wf.load_definitions(hub) if d.name == name), None)
    model = getattr(definition, "inputs", None)
    try:
        data = inputs_lib.parse_cli(pairs)
        limit = getattr(definition, "max_input_bytes", inputs_lib.MAX_BYTES)
        values = inputs_lib.validate(model, data or None, max_bytes=limit) if (model or data) else None
    except inputs_lib.InputError as exc:
        raise ValueError("Input " + str(exc)) from None
    dumped = inputs_lib.dump(values)
    return {"trigger": "cli", "scope": "team", "started_by": "cli",
            "inputs": dumped, "inputs_display": inputs_lib.display(model, dumped)}


def _print_report_links(hub: Path, run_id: str) -> None:
    from .artifacts import series as series_lib

    try:
        for s in series_lib.touched_by(hub, hub.name.lower(), run_id):
            console.print(f"Report: {series_lib.latest_url(hub, s)}")
    except Exception:  # noqa: BLE001 — links are a convenience
        pass


@schedule_app.command("run")
def schedule_run(
    hub: Path = typer.Argument(..., help="Hub directory."),
    task_name: str = typer.Argument(..., metavar="TASK", help="Task name (the md filename stem)."),
    timeout: int = typer.Option(None, "--timeout", help="Override the task's per-round timeout (seconds)."),
    max_rounds: int = typer.Option(None, "--max-rounds", help="Override the task's round cap."),
    model: str = typer.Option(None, "--model", help="Override the model for this run (LLM tasks), e.g. claude-local/opus."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the round-1 prompt (LLM) or the command (script) and exit."),
    inputs: list[str] = typer.Option(None, "--input", help="A workflow input as key=value. Repeat for each input."),
) -> None:
    """Fire one task NOW — for testing and manual runs.

    Uses the hub's configured MODEL (claude-local or any OpenAI/LiteLLM id),
    the same as a scheduler fire, and runs on the hub's DBOS engine on the same
    one-at-a-time queue, so it can't overlap a scheduled run. Ignores the cron
    schedule and the idle gate. Exit 0 = the agent reported DONE; 1 = otherwise.
    """
    import logging as _logging

    from . import schedule_runner as runner
    from . import scheduling as sch

    hub = hub.resolve()
    tasks, problems = sch.load_tasks(hub)
    by_name = {t.name: t for t in tasks}
    if task_name not in by_name:
        from .workflows.observe import definitions
        candidates = definitions(hub)
        want = task_name.replace('-', '_')
        match_def = next((w for w in candidates if w['name'] == want), None)
        if match_def and match_def['error']:
            console.print(f"[red]{match_def['error']}[/red]")
            raise typer.Exit(2)
        if match_def and any(v is not None for v in (timeout, max_rounds, model)):
            console.print("[red]Workflows do not accept --timeout, --max-rounds or --model. Configure these in workflow code.[/red]")
            raise typer.Exit(2)
        if inputs and not match_def:
            console.print("[red]--input is only for code workflows.[/red]")
            raise typer.Exit(2)
        if dry_run:
            if match_def is None:
                console.print(f"[red]No workflow {task_name!r} found[/red]")
                raise typer.Exit(2)
            console.print(f"Would run workflow {want} from {match_def['source']}; no code executed.")
            return
        envelope = None
        if match_def:
            try:
                envelope = _cli_envelope(hub, want, inputs)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(2)
        _load_settings(hub)
        # Second source: a DBOS workflow under <hub>/workflows/<name>/. The
        # registry is keyed by the function name; accept the folder name too
        # (hyphens/underscores interchangeable).
        try:
            from . import runtime as _agent_rt
            from .workflows import context as _wf_ctx
            from .workflows import runtime as _wf

            _wf_ctx.configure(
                llm=lambda spec, hub_dir=None, subject=None: _agent_rt.complete_once(hub_dir, spec, subject=subject),
                agent=lambda task, hub_dir=None, subject=None: _agent_rt.run_once(hub_dir, task, subject=subject),
                jev=lambda spec, hub_dir=None, subject=None: _agent_rt.jev_once(hub_dir, spec, subject=subject),
            )
            from .workflows.client import enqueue_live
            live = enqueue_live(hub, want, envelope=envelope)
            if live is not None:
                _audit_cli_start(hub, want, live.get_workflow_id())
                console.print(f"Workflow {want}: {live.get_result()!r}")
                _print_report_links(hub, live.get_workflow_id())
                return
            _wf.init(hub)
            _wf.load_workflows(hub)
            _wf.launch(target=want)
            wf_names = {w.name for w in _wf.registry()}
            want = task_name.replace("-", "_")
            match = next((n for n in wf_names if n == task_name or n == want), None)
            if match:
                console.print(f"[cyan]→ running workflow {match}[/cyan]")
                try:
                    handle = _wf.start(match, envelope=envelope)
                    _audit_cli_start(hub, match, handle.get_workflow_id())
                    result = handle.get_result()
                except Exception as run_exc:  # noqa: BLE001 — the run failed, not the load
                    console.print(f"[red]✗ workflow {match} failed: {type(run_exc).__name__}: {run_exc}[/red]")
                    console.print("[dim]The failed run is in `hubzoid schedule status` and the Console's Runs page.[/dim]")
                    raise typer.Exit(1)
                finally:
                    _wf.shutdown()
                console.print(f"[green]✓ workflow {match} returned:[/green] {result!r}")
                _print_report_links(hub, handle.get_workflow_id())
                return
            known_wf = ", ".join(sorted(wf_names))
            _wf.shutdown()
        except typer.Exit:
            raise
        except Exception as e:  # noqa: BLE001 — report, then fall through to the error
            _wf.shutdown()
            known_wf = f"(workflow load failed: {e})"

        known = ", ".join(sorted(by_name)) or "(none)"
        console.print(
            f"[red]no task or workflow {task_name!r} under {hub}. "
            f"Tasks: {known}. Workflows: {known_wf or '(none)'}[/red]"
        )
        for p in problems:
            console.print(f"[red]✗ {p}[/red]")
        raise typer.Exit(2)
    task = by_name[task_name]
    if timeout:
        task.timeout = timeout
    if max_rounds:
        task.max_rounds = max_rounds
    if model:
        if task.is_script:
            console.print("[yellow]--model is ignored for a script (run:) task — "
                          "scripts don't use an LLM.[/yellow]")
        else:
            task.model = model

    if dry_run:
        if task.is_script:
            display = task.run[0] if task.run_shell else " ".join(task.run or [])
            typer.echo(f"[script task] would run: {display}")
        else:
            typer.echo(runner.build_prompt(task, hub, round_no=1))
        return

    # Manual runs should be observable in the terminal.
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if task.is_script:
        console.print(f"[cyan]→ running {task.name}[/cyan] (script, timeout {task.timeout}s)")
    else:
        console.print(f"[cyan]→ running {task.name}[/cyan] (timeout {task.timeout}s/round, ≤{task.max_rounds} rounds)")
    # Queued on the hub's DBOS engine like a scheduled fire (the same one-at-a-
    # time markdown queue, so it can't overlap a scheduled run), then awaited.
    from datetime import datetime as _dt

    from .workflows import markdown as _md
    from .workflows import runtime as _wf

    overrides = {"timeout": timeout, "max_rounds": max_rounds,
                 "model": None if task.is_script else model}
    try:
        from .workflows.client import enqueue_live
        live = enqueue_live(hub, task.name, markdown=True, overrides=overrides)
        if live is not None:
            outcome = live.get_result()
            console.print(f"Task {task.name}: {outcome!r}")
            if outcome.get("result") != "done":
                raise typer.Exit(1)
            return
        _wf.init(hub)
        _wf.launch(target="md:"+task.name)
        handle = _md.enqueue_task(task.name, "manual-" + _dt.now().strftime("%Y%m%dT%H%M%S"),
                                  overrides=overrides)
        _audit_cli_start(hub, f"md:{task.name}", handle.get_workflow_id())
        try:
            outcome = handle.get_result()
        except Exception as exc:  # noqa: BLE001 — the run failed; report it
            console.print(f"[red]✗ {task.name} failed:[/red] {exc}")
            raise typer.Exit(1)
    finally:
        _wf.shutdown()
    console.print(f"[dim]run log: {outcome.get('run_log')}[/dim]")
    if outcome.get("result") == "done":
        sha = f" · committed {outcome['commit_sha'][:10]}" if outcome.get("commit_sha") else ""
        console.print(f"[green]✓ done in {outcome.get('rounds')} round(s)[/green]: "
                      f"{outcome.get('summary') or '(no summary)'}{sha}")
    else:
        console.print(f"[red]✗ {outcome.get('result')} after {outcome.get('rounds')} round(s)[/red] "
                      f"{outcome.get('error') or ''}")
        raise typer.Exit(1)


def _audit_cli_start(hub: Path, name: str, run_id: str) -> None:
    """Record a manual run in the access audit (`run_start`, surface `cli`), as
    the agent tools do. A failed write is logged; the run goes ahead."""
    import logging

    try:
        from .access import store_for

        store_for(hub).audit_run_control(hub.name, "run_start", name, actor=_operator(),
                                         surface="cli", subject=run_id)
    except Exception:  # noqa: BLE001
        logging.getLogger("hubzoid.cli").exception("could not audit the manual run of %s", name)


def _operator() -> str:
    """Who ran a control command: the server account, recorded in the audit.
    Run controls act with the authority of whoever can run commands here."""
    import getpass
    import socket

    return f"cli:{getpass.getuser()}@{socket.gethostname()}"


def _no_such_target(hub: Path, name: str) -> None:
    """Say the name matches nothing here and exit 2 (never returns)."""
    shown = name[3:] if name.startswith("md:") else name
    console.print(f"[red]no task or workflow {shown!r} under {hub}[/red]")
    raise typer.Exit(2)


def _schedule_target(hub: Path, name: str) -> str:
    """Resolve a task or workflow name to its stored form (`md:<task>` for a
    markdown task, the function name for a code workflow)."""
    from .workflows import control

    try:
        return control.resolve(hub, name).name
    except control.ControlError:
        _no_such_target(hub, name)


@schedule_app.command("pause")
def schedule_pause(
    hub: Path = typer.Argument(..., help="Hub directory."),
    name: str = typer.Argument(..., help="Markdown task (schedule/<name>.md) or code workflow name."),
) -> None:
    """Stop scheduled runs of one task or workflow until resumed. Runs already
    queued or running are not stopped (use `cancel`); manual runs still work.
    Recorded in the access audit."""
    from .workflows import control

    hub = hub.resolve()
    try:
        done = control.set_paused(hub, name, True, actor=_operator(), surface="cli")
    except control.ControlError:
        _no_such_target(hub, name)
    console.print(f"[yellow]paused[/yellow] {done['workflow']} in {hub.name}. "
                  f"Resume with: hubzoid schedule resume {hub} {name}")


@schedule_app.command("resume")
def schedule_resume(
    hub: Path = typer.Argument(..., help="Hub directory."),
    name: str = typer.Argument(..., help="Markdown task or code workflow name."),
) -> None:
    """Resume scheduled runs. A markdown task that became due while paused runs
    once (the same catch-up as after downtime); code workflows don't back-fill."""
    from .workflows import control

    hub = hub.resolve()
    try:
        done = control.set_paused(hub, name, False, actor=_operator(), surface="cli")
    except control.ControlError:
        _no_such_target(hub, name)
    console.print(f"[green]resumed[/green] {done['workflow']} in {hub.name}")


@schedule_app.command("cancel")
def schedule_cancel(
    hub: Path = typer.Argument(..., help="Hub directory."),
    run_id: str = typer.Argument(..., help="The run id (from `hubzoid schedule status` or the Console)."),
) -> None:
    """Cancel a queued or running run. Best effort: a run stops at its next
    step boundary, and work already done (a sent message, a git push, a script's
    effects) is not undone. Recorded in the access audit."""
    from .workflows import control

    hub = hub.resolve()
    try:
        control.cancel(hub, run_id, actor=_operator(), surface="cli")
    except control.ControlError as exc:
        if exc.code == "finished":
            console.print(f"[yellow]{run_id} is already {exc.status}; nothing to cancel[/yellow]")
            return
        console.print(f"[red]no run {run_id!r} in {hub.name}. "
                      f"List runs with: hubzoid schedule status {hub}[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]cancel requested[/yellow] for {run_id} (stops at its next step)")


@schedule_app.command("status")
def schedule_status(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Print the status as JSON."),
) -> None:
    """Show each task's recorded fire history (anchors, last result, last log)."""
    from . import scheduling as sch

    hub = hub.resolve()
    tasks, _ = sch.load_tasks(hub)
    state = sch.ScheduleState(hub)
    from .workflows.observe import catalog, runs
    workflows = catalog(hub)
    if as_json:
        try:
            recent, history_error = runs(hub, limit=20, trusted=True), None
        except Exception as exc:  # noqa: BLE001
            recent, history_error = None, str(exc)
        print(json.dumps({"workflows": workflows, "runs": recent, "history_error": history_error,
                          "tasks": {t.name: {"enabled": t.enabled, **state.get(t.name)} for t in tasks}},
                         indent=2, default=str))
        return
    for w in workflows:
        console.print(f"workflow {w['name']} · {w['state']} · next {w['next_run']} · missed {w['missed']}")
        if w['error']:
            console.print(f"[red]{w['error']}[/red]")
    try:
        # The server's operator can read the run database directly anyway.
        for r in runs(hub, limit=20, trusted=True):
            console.print(f"{r['id']} · {r['name']} · {r['status']} · {r['duration_ms']}ms")
            if r['error']:
                console.print(f"[red]{r['error']}[/red]")
    except Exception as exc:
        console.print(f"[red]Workflow history unavailable: {exc}[/red]")
    if not tasks and not workflows:
        console.print("No scheduled tasks or workflows.")
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


@schedule_app.command("redrive")
def schedule_redrive(
    event_id: str = typer.Argument(..., help="Failed webhook event id (wh:...)."),
    hub: Path = typer.Option(Path("."), "--hub", help="Hub directory."),
) -> None:
    """Retry a failed event after checking its external side effects."""
    from .workflows.events import redrive
    hub = hub.resolve()
    try:
        redrive(hub, hub.name, event_id)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print("Event accepted for redrive; the live engine will dispatch it.")


@schedule_app.command("deliveries")
def schedule_deliveries(hub: Path = typer.Argument(Path(".")),
                        as_json: bool = typer.Option(False, "--json", help="Print the deliveries as JSON.")) -> None:
    """Inspect event and alert delivery metadata without private payloads."""
    from sqlalchemy import text
    from .workflows.events import _engine
    hub = hub.resolve()
    out: dict = {}
    with _engine(hub).connect() as conn:
        for table in ('hz_workflow_events', 'hz_workflow_alerts'):
            rows = conn.execute(text(f'SELECT id,state,attempt,error FROM {table} WHERE hub=:h ORDER BY created DESC LIMIT 50'), {'h':hub.name}).mappings()
            out[table.removeprefix("hz_workflow_")] = [dict(r) for r in rows]
    if as_json:
        print(json.dumps(out, indent=2, default=str))
        return
    for items in out.values():
        for row in items:
            console.print(f"{row['id']} · {row['state']} · attempt {row['attempt']} · {row['error'] or ''}")


app.add_typer(
    schedule_app,
    name="schedule",
    help="Inspect / manually fire the hub's scheduled background tasks.",
    rich_help_panel="Commands",
)


# ---------------------------------------------------------------------------
# access — per-hub permissions on the one Casbin store (direct grants)
# ---------------------------------------------------------------------------
def _access_domain(hub_dir: Path, hub: str | None, org: bool) -> str:
    from .access.store import ORG

    if org:
        return ORG
    return hub or hub_dir.resolve().name


@app.command()
def grant(
    subject: str = typer.Argument(..., help="Who to grant (email or workflow:<name>)."),
    permission: str = typer.Argument(..., help="Permission, e.g. crm_read, use_hub, manage_access."),
    hub: str = typer.Option(None, "--hub", help="Hub (Casbin domain). Default: the hub dir's name."),
    org: bool = typer.Option(False, "--org", help="Grant org-wide (all hubs), for manage_access."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory (where the DB lives). Default: current dir."),
) -> None:
    """Grant a permission. Granting any tool permission auto-grants use_hub.
    New access for everyone signed in ('*') is refused: grant named people."""
    from .access import store_for
    from .access.store import BroadAccessRefused

    import getpass

    from .db import operational_url
    from sqlalchemy.engine import make_url
    if subject.strip() == "*":
        console.print("[red]refused:[/red] new access for everyone signed in can't be created. "
                      "Grant named people (their email) or workflow:<name> identities instead. "
                      "An existing one can still be removed with: hubzoid revoke '*' use_hub --hub <hub>")
        raise typer.Exit(code=1)
    console.print(f"Access store: {make_url(operational_url(hub_dir)).render_as_string(hide_password=True)}")
    domain = _access_domain(hub_dir, hub, org)
    try:
        store_for(hub_dir).grant(subject, domain, permission, actor=f"cli:{getpass.getuser()}")
    except (BroadAccessRefused, ValueError) as e:
        console.print(f"[red]refused:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]granted[/green] {subject} · {permission} in {domain}")


@app.command()
def revoke(
    subject: str = typer.Argument(..., help="Who to revoke from."),
    permission: str = typer.Argument(..., help="Permission to remove. use_hub removes the hub's perms."),
    hub: str = typer.Option(None, "--hub", help="Hub (Casbin domain). Default: the hub dir's name."),
    org: bool = typer.Option(False, "--org", help="Revoke an org-wide grant."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Revoke a permission. Refuses to remove the last org admin."""
    from .access import store_for
    from .access.store import LastAdminError

    import getpass

    from .db import operational_url
    from sqlalchemy.engine import make_url
    console.print(f"Access store: {make_url(operational_url(hub_dir)).render_as_string(hide_password=True)}")
    domain = _access_domain(hub_dir, hub, org)
    try:
        store_for(hub_dir).revoke(subject, domain, permission, actor=f"cli:{getpass.getuser()}")
    except LastAdminError as e:
        console.print(f"[red]refused:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[yellow]revoked[/yellow] {subject} · {permission} in {domain}")


access_app = typer.Typer(help="Per-hub access: check, list, bootstrap.", no_args_is_help=True)


@access_app.command("check")
def access_check(
    subject: str = typer.Argument(..., help="Who to check."),
    hub: str = typer.Option(None, "--hub", help="Hub (Casbin domain). Default: the hub dir's name."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Show every permission a subject effectively holds in a hub."""
    from .access import store_for

    domain = hub or hub_dir.resolve().name
    perms = sorted(store_for(hub_dir).permissions_for(subject, domain))
    if as_json:
        print(json.dumps({"subject": subject, "hub": domain, "permissions": perms}, indent=2))
        return
    if not perms:
        console.print(f"[dim]{subject} has no access in {domain}[/dim]")
        return
    console.print(f"{subject} in [bold]{domain}[/bold]: " + ", ".join(perms))


@access_app.command("list")
def access_list(
    hub: str = typer.Option(None, "--hub", help="Only this hub (Casbin domain)."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Print the grants as JSON."),
) -> None:
    """List all grants (subject · permission · hub)."""
    from .access import store_for

    rows = store_for(hub_dir).list_grants(hub)
    if as_json:
        print(json.dumps({"grants": [{"subject": s_, "hub": d_, "permission": p_} for s_, d_, p_ in rows]},
                         indent=2))
        return
    if not rows:
        console.print("[dim]no grants[/dim]")
        return
    for subject, dom, perm in rows:
        console.print(f"{subject:30.30}  {perm:20.20}  [dim]{dom}[/dim]")
    console.print(f"[dim]{len(rows)} grant(s)[/dim]")


@access_app.command("bootstrap")
def access_bootstrap(
    admin: list[str] = typer.Option([], "--admin", help="Subject to make an org admin (repeatable)."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """First-boot bootstrap: grant org admins once (idempotent). Break-glass for
    a deployment nobody can administer."""
    from .access import store_for

    store_for(hub_dir).bootstrap(admin)
    console.print(f"[green]bootstrapped[/green] admins={list(admin) or '(none)'}")


app.add_typer(
    access_app,
    name="access",
    help="Per-hub access: check, list, bootstrap.",
    rich_help_panel="Commands",
)


_WORKFLOW_TEMPLATE = '''\
"""The {name} workflow. A manual example with durable steps and state.

A workflow coordinates steps, calls agents, retains state, retries, and resumes
after a restart. Add a schedule only when ready. Read a secret INSIDE a step,
never at the top (step inputs are checkpointed).
"""
from hubzoid import workflow, step, hub


@workflow()  # Manual first. Add a schedule and timezone when ready.
def {func}():
    # example: durable, idempotent work
    seen = hub.state.get("seen", 0)
    result = {{"message": "Your workflow is ready"}}
    do_something(result)
    hub.state["seen"] = seen + 1
    return seen + 1


@step   # a durable side effect — make it idempotent (at-least-once)
def do_something(result):
    print(f"[{name}] {{result}}")
'''


new_app = typer.Typer(help="Scaffold new hub parts.", no_args_is_help=True)


@new_app.command("workflow")
def new_workflow(
    name: str = typer.Argument(..., help="Workflow name, e.g. review-prs."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Scaffold workflows/<name>/main.py with a runnable example."""
    func = _slugify(name).replace("-", "_")
    wf_dir = hub_dir.resolve() / "workflows" / name
    if wf_dir.exists():
        console.print(f"[red]already exists:[/red] {wf_dir}")
        raise typer.Exit(1)
    wf_dir.mkdir(parents=True)
    body = _WORKFLOW_TEMPLATE.format(name=name, func=func)
    (wf_dir / "main.py").write_text(body)
    console.print(f"[green]created[/green] {wf_dir / 'main.py'}")
    console.print(
        "[dim]run it once: [/dim]"
        f"hubzoid schedule run {shlex.quote(str(hub_dir))} {func}"
    )


app.add_typer(
    new_app,
    name="new",
    help="Scaffold new hub parts (workflow, ...).",
    rich_help_panel="Commands",
)

# Accounts for the Hubzoid web app (hubzoid/auth/cli.py) and the move from an
# Open WebUI install (hubzoid/migrate_openwebui.py). Each lives in its own module.
from .auth.cli import admin_app  # noqa: E402
from .chat.config_cli import chat_app  # noqa: E402
from .chat import engine_cli as _chat_engine_cli  # noqa: E402
from .secretstore.cli import secrets_app  # noqa: E402
from .hubs.cli import hub_app  # noqa: E402

_chat_engine_cli.register(chat_app)
from .chat import feedback_cli as _chat_feedback_cli  # noqa: E402

_chat_feedback_cli.register(chat_app)
from .chat import sharing_cli as _chat_sharing_cli  # noqa: E402

_chat_sharing_cli.register(chat_app)
from .chat import organization_cli as _chat_organization_cli  # noqa: E402

_chat_organization_cli.register(chat_app)
from .chat import devices_cli as _chat_devices_cli  # noqa: E402

_chat_devices_cli.register(chat_app)
from .ui.branding_cli import branding_app  # noqa: E402
from .admin.cli import register as _register_admin_cli  # noqa: E402
from .parity.cli import parity_app  # noqa: E402

_register_admin_cli(admin_app)
from .migrate_openwebui import migrate_app  # noqa: E402
from .migration_readiness import register as _register_readiness  # noqa: E402

_register_readiness(migrate_app)

app.add_typer(
    parity_app,
    name="parity",
    help="The chat parity ledger: validate, coverage, candidate, review, freeze.",
    rich_help_panel="Commands",
)
app.add_typer(
    branding_app,
    name="branding",
    help="Deployment branding: draft, preview, review, activate, roll back.",
    rich_help_panel="Commands",
)
app.add_typer(
    chat_app,
    name="chat",
    help="Chat settings, theme and personal preferences.",
    rich_help_panel="Commands",
)
from .chat.files_cli import artifacts_app as _artifacts_app, files_app as _files_app  # noqa: E402

app.add_typer(_files_app, name="files", help="Chat files: upload, reuse, download, delete, storage doctor.",
              rich_help_panel="Commands")
app.add_typer(_artifacts_app, name="artifacts", help="Documents made in conversations and their versions.",
              rich_help_panel="Commands")
from .chat.knowledge_cli import knowledge_app as _knowledge_app  # noqa: E402
from .chat.models_cli import models_app as _models_app  # noqa: E402
from .chat.authoring_cli import workspace_app as _workspace_app  # noqa: E402

app.add_typer(_workspace_app, name="workspace", help="Draft, review and publish agent, skill and prompt files.",
              rich_help_panel="Commands")
from .hitl.cli import hitl_app as _hitl_app, requests_app as _requests_app  # noqa: E402

app.add_typer(_hitl_app, name="hitl", help="When the agent asks a person first (hitl.yaml).",
              rich_help_panel="Commands")
app.add_typer(_requests_app, name="requests", help="Approvals and questions waiting for a person.",
              rich_help_panel="Commands")
from .workflows.access_cli import app as _workflow_access_app  # noqa: E402

app.add_typer(_workflow_access_app, name="workflow-access",
              help="Who may run, schedule, edit, approve and see each workflow.", rich_help_panel="Commands")
from .hub_changes.cli import app as _hub_changes_app  # noqa: E402

app.add_typer(_hub_changes_app, name="hub-changes",
              help="Proposed changes to a hub's own instructions and knowledge.", rich_help_panel="Commands")

app.add_typer(_models_app, name="models", help="Models people may pick, and presets.", rich_help_panel="Commands")

app.add_typer(_knowledge_app, name="knowledge", help="Your sources: collections, sync, query, citations, safe fetch.",
              rich_help_panel="Commands")
app.add_typer(
    admin_app,
    name="admin",
    help="Accounts: create administrators and users, reset passwords.",
    rich_help_panel="Commands",
)
from .auth.remote_cli import api as _api_command, auth_app as _auth_app  # noqa: E402

app.add_typer(
    _auth_app,
    name="auth",
    help="Your account on a hosted Hubzoid: sign in, API keys, devices, two-step sign-in.",
    rich_help_panel="Commands",
)
app.command("api", rich_help_panel="Commands")(_api_command)
app.add_typer(
    migrate_app,
    name="migrate",
    help="Move an Open WebUI install to the Hubzoid web app.",
    rich_help_panel="Commands",
)
app.add_typer(
    hub_app,
    name="hub",
    help="A hub's settings (settings.yaml, hub.settings.yaml) and its own checks.",
    rich_help_panel="Commands",
)
app.add_typer(
    secrets_app,
    name="secrets",
    help="Secrets stored for the Console: list, set and delete. Values are never shown.",
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
_STARTER_MODEL_LINE = "MODEL=claude-local              # defaults to Sonnet 4.x (decisive on routing rules)"
_STARTER_BRIDGE_KEY_LINE = "# BRIDGE_API_KEYS=dev           # comma-separated keys for the OpenAI-compatible /v1 API"

_STARTER_ENV = """\
# Hub configuration. This file is git-ignored and can hold keys: keep it private.
# Secrets saved in the Console or with `hubzoid secrets set` are stored in the
# database, not here. `hubzoid secrets list` shows both. A value in this file wins.
#
# --- Model -----------------------------------------------------------------
# The default uses your installed `claude` CLI and Pro/Max subscription for
# inference. No API key needed. Requires `claude login` already done.
#
# No `claude login` here? Use a hosted provider: comment out MODEL=claude-local,
# uncomment one stanza below and set its key. (`hubzoid init` in a terminal
# offers to do this for you.)

""" + _STARTER_MODEL_LINE + """
# MODEL=codex-local            # Codex CLI login; see docs/providers.md for supported version
# MODEL=codex-local/<model-id> # optional Codex model pin
# MODEL=claude-local/sonnet     # explicit; same as bare `claude-local`
# MODEL=claude-local/opus       # opt in to Opus
# MODEL=claude-local/haiku      # opt in to Haiku (~3x faster TTFT, but tends to ask before executing documented workflows)

# Headless / server (no interactive `claude login` on the box): paste a
# subscription token minted with `claude setup-token`. It is NOT an API key
# and is NOT billed per token: usage draws on your Pro/Max subscription.
# The `claude` CLI reads it automatically. See docs/DEPLOYING.md §5b.
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

# --- OpenRouter (one key, many models) -------------------------------------
# OPENROUTER_API_KEY=
# MODEL=openrouter/anthropic/claude-haiku-4.5
# Tip: at https://openrouter.ai/settings/preferences pin Anthropic first
# (allow fallbacks). Otherwise OpenRouter splits calls across Anthropic /
# Vertex / Bedrock and prompt cache hits get fragmented.

# --- Anthropic --------------------------------------------------------------
# ANTHROPIC_API_KEY=
# MODEL=anthropic/claude-haiku-4-5

# --- OpenAI -----------------------------------------------------------------
# OPENAI_API_KEY=
# MODEL=openai/gpt-4o-mini

# --- Jev decisions (hub.call_jev and the call_jev chat tool, experimental) --
# A dedicated OpenRouter key used only for Jev. OPENROUTER_API_KEY above is
# never used for Jev, and the chat model never uses this key. The chat tool
# stays off until the jev capability is granted in the Console.
# JEV_OPENROUTER_API_KEY=

# --- Web app and sign-in ----------------------------------------------------
# `hubzoid run` serves the web app, file downloads and MCP on one port.
# Sign-in is off by default (local mode): whoever opens the page is the hub's
# owner, so the port stays on this machine (127.0.0.1). Turn sign-in on before
# exposing it with --host 0.0.0.0 or behind a proxy.
# HUBZOID_AUTH=true                    # people sign in with Hubzoid accounts
# HUBZOID_PUBLIC_URL=https://hub.example.com  # the address people open; needed
                                       # behind a proxy and for Google sign-in
# HUBZOID_ADMIN_EMAIL=you@example.com  # bootstraps the first administrator;
# HUBZOID_ADMIN_PASSWORD=              # remove both lines after the first start
# ENABLE_SIGNUP=false                  # people cannot create their own accounts

# Google sign-in (with sign-in on). Accounts are created by administrators;
# Google sign-in attaches to the account with the same email.
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
# OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true
# OAUTH_ALLOWED_DOMAINS=your-company.com
# Authorized redirect URI in Google Console: <HUBZOID_PUBLIC_URL>/oauth/google/callback

# Hosted MCP (Claude Code, Cursor and other MCP clients use this hub's tools and
# knowledge). On by default for a local run and for an https HUBZOID_PUBLIC_URL;
# `hubzoid run` prints the `claude mcp add` line.
# MCP_SERVER=false

# Logo and browser tab icon: put logo.svg (or logo.png) and favicon.svg in ./branding/.

# --- Bridge and ports (all optional) ---------------------------------------
""" + _STARTER_BRIDGE_KEY_LINE + """
# MODEL_LABEL=                  # what /v1/models reports; blank = derived from AGENTS.md name
# PORT=3080                     # public port: web app, downloads, MCP
# BRIDGE_PORT=8000              # FastAPI bridge port, always on 127.0.0.1
# HTTP_ALLOWLIST=               # comma-separated hostnames the http_get tool may visit
# HUBZOID_DISABLE_HTTP_GET=true # remove http_get from the tool registry entirely
# HUBZOID_DISABLE_WEB_SEARCH=true  # remove web_search from the tool registry entirely

# --- Workflows: who they run as, reports and email ------------------------
# Scheduled workflows and schedule/*.md tasks run as an ordinary account. A
# declaration's run_as wins; otherwise this setting; otherwise the owner
# recorded at setup (locally: admin@localhost). See docs/workflow-identity.md.
# HUBZOID_WORKFLOW_USER=reports@company.com
# Owner email from workflows (hub.send_email): usually set once for the whole
# deployment. See docs/reports-and-email.md.
# HUBZOID_SMTP_HOST=smtp.company.com
# HUBZOID_SMTP_PORT=587
# HUBZOID_SMTP_USERNAME=
# HUBZOID_SMTP_PASSWORD=
# HUBZOID_SMTP_FROM=hubzoid@company.com
# HUBZOID_SMTP_STARTTLS=true
# HUBZOID_EMAIL_DELIVERY=preview  # write emails to .hubzoid/outbox/ instead of sending

# --- Slack chat surface (optional, opt-in per agent) ----------------------
# Run `hubzoid slack manifest .` to generate an App Manifest you can paste
# into https://api.slack.com/apps. After installing the app to your workspace
# copy the two tokens here, then run `hubzoid slack run .` next to your hub.
# Full walkthrough: docs/slack.md.
# SLACK_BOT_TOKEN=xoxb-...        # Bot User OAuth Token
# SLACK_APP_TOKEN=xapp-...        # App-Level Token, scope connections:write
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
        ".hubzoid/\n"
        ".openwebui-data/\n"
        ".webui_secret_key\n"
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


def _template_root(name: str = DEFAULT_TEMPLATE) -> Path | None:
    """Return the on-disk path of a bundled template, or None.

    Templates live at `hubzoid/templates/<name>/`: `operations` (the default
    example), `minimal` (one example per file type), `demo` (the guided tour)
    and `watchtower` (workflow-first).
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


def _wait_for(url: str, timeout: float = 60.0, *, expected_hub: str | None = None,
              expected_model: str | None = None) -> bool:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if expected_hub and r.status_code == 200:
                try:
                    health = r.json()
                except ValueError:
                    health = None
                # Older bridges report the hub but not the model. A reported
                # mismatch may be a bridge still restarting during an upgrade;
                # keep polling until the deadline, without accepting the wrong hub.
                if (isinstance(health, dict) and health.get("hub") == expected_hub
                        and (expected_model is None or health.get("model") is None
                             or health.get("model") == expected_model)):
                    return True
            if not expected_hub and 200 <= r.status_code < 400:
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



# ---------------------------------------------------------------------------
# eval — hub-owned behavioural checks (see hubzoid/evals/)
# ---------------------------------------------------------------------------
eval_app = typer.Typer(
    help="Hub-owned evals: one md file per case under <hub>/evals/. Run them "
    "by hand, from CI (the exit code is the gate), or on a cron via a case's "
    "`schedule:` frontmatter.",
    no_args_is_help=True,
)


class _JsonQuiet:
    """Swallows progress output while --json prints only the JSON record."""

    def __init__(self, _console):
        pass

    def print(self, *_args, **_kwargs) -> None:
        pass


def _eval_fail(message: str, code: int, as_json: bool, error_code: str = "invalid_request") -> None:
    if as_json:
        print(json.dumps({"detail": {"code": error_code, "message": message}}))
    else:
        console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code)


def _load_cases(hub: Path, tag: str | None, case: str | None, *, as_json: bool = False):
    """Discover + filter, turning a bad case file into a clean CLI error."""
    from .evals import cases as cases_lib

    try:
        found = cases_lib.discover(hub)
    except cases_lib.EvalCaseError as exc:
        _eval_fail(str(exc), 2, as_json, "invalid_case")

    if not found:
        if as_json:
            print(json.dumps({"cases": [], "message": "no eval cases"}))
        else:
            console.print(f"no eval cases — add markdown files under {hub / 'evals'}/")
        raise typer.Exit(0)

    selected = cases_lib.select(found, tag=tag, case=case)
    if not selected:
        if as_json:
            print(json.dumps({"cases": [], "message": "no cases matched that filter"}))
        else:
            console.print("[yellow]no cases matched that filter[/yellow]")
        raise typer.Exit(0)
    return selected


def _case_json(c, hub: Path) -> dict:
    return {"id": c.name, "path": str(c.source_path.relative_to(hub)) if c.source_path else None,
            "tags": list(c.tags), "enabled": c.enabled, "turns": len(c.turns) or 1,
            "judged": c.is_judged, "schedule": c.schedule, "run_as": c.run_as,
            "expect_tools": list(c.expect_tools), "forbid_tools": list(c.forbid_tools),
            "expect_tool_args": sorted(c.expect_tool_args), "contains": len(c.contains),
            "not_contains": len(c.not_contains), "fake_tools": [f.tool for f in c.fake_tools],
            "real_tools": list(c.real_tools), "digest": c.digest}


@eval_app.command("run")
def eval_run(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    tag: str = typer.Option(None, "--tag", help="Only cases carrying this tag."),
    case: str = typer.Option(None, "--case", help="Only cases whose name matches this glob."),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the grading call. The agent still runs, so this is cheaper, not free."),
    judge_model: str = typer.Option(None, "--judge-model", help="Model that grades. Default: HUBZOID_EVAL_JUDGE_MODEL, else the hub's own."),
    model: str = typer.Option(None, "--model", help="Override the model under test for this run."),
    compare: bool = typer.Option(False, "--compare", help="Also diff against the previous run."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the summary and failures."),
    details: bool = typer.Option(False, "--details", help="Under each case, one line per tool call: its key arguments, ok or failed, and time."),
    run_as: str = typer.Option(None, "--run-as", help="Run cases as this account (an email), with the workflow identity rules. A case's own run_as wins. Grants nothing."),
    junit: Path = typer.Option(None, "--junit", help="Also write a JUnit XML report to this file, for CI."),
    as_json: bool = typer.Option(False, "--json", help="Print the results as JSON (the same record as the results file) and nothing else."),
) -> None:
    """Run the hub's eval cases. Exits non-zero if any fail — that is the CI gate.

    Cases run through the hub's own runtime, so they see the same model, tools,
    MCP servers and access guard that real chat traffic does. Free checks
    (substrings, tool calls and their arguments) run first; a case with a
    `## Criteria` section is then graded by a model, but only if the free
    checks passed.
    """
    import os

    from .evals import judge as judge_lib
    from .evals import report as report_lib
    from .evals import suite as suite_lib
    from .evals.cases import EvalCaseError

    hub = hub.resolve()
    try:
        run_as = suite_lib.check_run_as(run_as)
    except EvalCaseError as exc:
        _eval_fail(f"--run-as: {exc}", 2, as_json)
    selected = _load_cases(hub, tag, case, as_json=as_json)
    out = _JsonQuiet(console) if as_json else console

    judged = [c for c in selected if c.is_judged]
    out.print(f"[cyan]{len(selected)} case(s)[/cyan] · {hub.name}")
    if judged and not no_judge:
        out.print(f"[dim]judge: {escape(judge_lib.describe(hub, judge_model))}[/dim]")
    elif judged:
        out.print(f"[dim]judge: off — {len(judged)} case(s) will run free checks only[/dim]")
    if run_as:
        out.print(f"[dim]run as: {escape(run_as)} (a case's own run_as wins)[/dim]")

    def _progress(result) -> None:
        if as_json or (quiet and result.passed):
            return
        mark = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
        console.print(f"  {mark} {escape(result.name)}"
                      + (f"  [dim]{escape(result.reason)}[/dim]" if result.reason else ""))
        if details:
            for line in report_lib.detail_lines(result):
                console.print(f"    [dim]{escape(line)}[/dim]", soft_wrap=True)

    # CI systems set CI=true; the trigger is recorded in the results file.
    trigger = "ci" if (os.environ.get("CI") or "").strip().lower() in ("1", "true", "yes") else "cli"
    previous = report_lib.load_runs(hub, limit=1)
    try:
        path, suite = suite_lib.run_and_save(
            hub, [c.name for c in selected], judge=not no_judge, run_as=run_as,
            trigger=trigger, model=model, judge_model=judge_model,
            on_case=_progress, push=False)
    except EvalCaseError as exc:      # a case file changed under us
        _eval_fail(str(exc), 2, as_json)

    if junit is not None:
        from .evals import junit as junit_lib

        junit_lib.write(suite, junit)
    if as_json:
        from .evals.calls import redact

        _push_to_langfuse(hub, suite, Console(stderr=True))  # stdout holds only the JSON
        print(json.dumps({"path": str(path), **(junit and {"junit": str(junit)} or {}), **redact(suite.to_dict())},
                         indent=2, default=str))
        raise typer.Exit(0 if suite.ok else 1)

    console.print()
    report_lib.render_table(console, suite)
    console.print(f"[dim]{path}[/dim]")
    if junit is not None:
        console.print(f"[dim]junit: {junit}[/dim]")

    _push_to_langfuse(hub, suite)

    if compare:
        console.print()
        if previous:
            prev_path, prev = previous[-1]
            report_lib.render_compare(
                console, report_lib.compare(prev, suite), prev_name=prev_path.stem)
        else:
            console.print("[dim]no previous run to compare against[/dim]")

    if not suite.ok:
        raise typer.Exit(1)


def _push_to_langfuse(hub: Path, suite, out: Console | None = None) -> None:
    """Best-effort push. Never fails a run — the local JSON is the record."""
    from .evals import langfuse as lf

    out = out or console
    try:
        pushed = lf.push(hub, suite)
    except Exception as exc:  # noqa: BLE001
        out.print(f"[yellow]langfuse push skipped: {escape(str(exc))}[/yellow]")
        return
    if pushed:
        out.print(f"[dim]langfuse: {pushed}[/dim]")


@eval_app.command("list")
def eval_list(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List the hub's eval cases: what each checks, and which are scheduled."""
    from . import scheduling as sch
    from .evals import cases as cases_lib

    hub = hub.resolve()
    try:
        found = cases_lib.discover(hub)
    except cases_lib.EvalCaseError as exc:
        _eval_fail(str(exc), 2, as_json, "invalid_case")

    if as_json:
        print(json.dumps({"hub": hub.name, "cases": [_case_json(c, hub) for c in found]}, indent=2))
        return
    if not found:
        console.print(f"no eval cases — add markdown files under {hub / 'evals'}/")
        return

    for c in found:
        bits = []
        if c.turns:
            bits.append(f"{len(c.turns)} turns")
        if c.expect_tools:
            bits.append(f"expects {', '.join(c.expect_tools)}")
        if c.expect_tool_args:
            bits.append(f"checks arguments of {', '.join(c.expect_tool_args)}")
        if c.forbid_tools:
            bits.append(f"forbids {', '.join(c.forbid_tools)}")
        if c.contains:
            bits.append(f"contains {len(c.contains)}")
        if c.not_contains:
            bits.append(f"not_contains {len(c.not_contains)}")
        bits.append("judged" if c.is_judged else "free only")
        if c.run_as:
            bits.append(f"runs as {c.run_as}")
        if c.fake_tools:
            bits.append(f"fakes {', '.join(sorted({f.tool for f in c.fake_tools}))}")
        if c.tags:
            bits.append(f"tags: {', '.join(c.tags)}")
        if not c.enabled:
            console.print(f"[dim]⏸ {c.name}  disabled[/dim]")
            continue
        when = ""
        if c.cron is not None:
            when = f"  {c.schedule} ({sch.cron_to_human(c.cron)})"
        console.print(f"[green]●[/green] [bold]{c.name}[/bold]{when}  ·  {'; '.join(bits)}")


@eval_app.command("status")
def eval_status(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Last run, pass rate, and what is currently failing.

    This is the surface that matters for scheduled evals — nobody is watching
    a terminal at 06:00 on a Monday.
    """
    from .evals import report as report_lib

    hub = hub.resolve()
    suite = report_lib.latest(hub)
    if as_json:
        if suite is None:
            print(json.dumps({"hub": hub.name, "runs": 0}))
            return
        print(json.dumps({"hub": hub.name, "ok": suite.ok, "passed": suite.passed, "failed": suite.failed,
                          "total": len(suite.cases), "finished_at": suite.finished_at or suite.started_at,
                          "model": suite.model, "judge_model": suite.judge_model, "trigger": suite.trigger,
                          "run_as": suite.run_as,
                          "failing": [{"id": c.name, "reason": c.reason} for c in suite.cases if not c.passed]},
                         indent=2))
        return
    if suite is None:
        console.print("no eval runs yet — try `hubzoid eval run`")
        return

    verdict = "[green]all passing[/green]" if suite.ok else f"[red]{suite.failed} failing[/red]"
    console.print(f"{verdict}  ·  {suite.passed}/{len(suite.cases)} passed"
                  f"  ·  {suite.finished_at or suite.started_at}")
    console.print(f"[dim]model: {suite.model or '?'}"
                  + (f" · judge: {suite.judge_model}" if suite.judge_model else "")
                  + (f" · trigger: {suite.trigger}" if suite.trigger else "")
                  + (f" · run as: {suite.run_as}" if suite.run_as else "")
                  + "[/dim]")
    for c in suite.cases:
        if not c.passed:
            console.print(f"  [red]✗[/red] {c.name}  [dim]{c.reason}[/dim]")


@eval_app.command("explain")
def eval_explain(
    hub: Path = typer.Argument(..., help="Hub directory."),
    case_name: str = typer.Argument(..., metavar="CASE", help="Case id: its path under evals/ without .md."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Everything needed to fix one failing case, in one place.

    The prompt, the full response, the tool calls, each assertion's verdict,
    the judge's reasoning, and the path to the markdown to edit — because
    "edit the instructions" is the primary fix lever, not "edit the code".
    """
    from .evals import cases as cases_lib
    from .evals import report as report_lib
    from .evals.calls import describe as calls_describe

    hub = hub.resolve()
    suite = report_lib.latest(hub)
    if suite is None:
        _eval_fail("no eval runs yet — try `hubzoid eval run`", 1, as_json, "not_found")

    result = next((c for c in suite.cases if c.name == case_name), None)
    if result is None:
        known = ", ".join(c.name for c in suite.cases) or "(none)"
        _eval_fail(f"no case {case_name!r} in the last run. Ran: {known}", 2, as_json, "not_found")

    case = next((c for c in cases_lib.discover(hub, strict=False) if c.name == case_name), None)
    if as_json:
        print(json.dumps({"case_file": str(case.source_path) if case and case.source_path else None,
                          "stale": bool(case and result.case_digest and case.digest != result.case_digest),
                          "run_finished_at": suite.finished_at or suite.started_at, **result.to_dict()},
                         indent=2, default=str))
        return

    console.print(f"[bold]{result.name}[/bold]  "
                  + ("[green]PASS[/green]" if result.passed else "[red]FAIL[/red]")
                  + f"  ·  {result.duration:.1f}s")
    if case is not None and case.source_path:
        console.print(f"[dim]case file:  {case.source_path}[/dim]")
    console.print(f"[dim]hub spec:   {hub / 'AGENTS.md'}[/dim]")

    if result.run_as:
        console.print(f"[dim]ran as:     {escape(result.run_as)}[/dim]")

    if result.turns is not None:
        for n, turn in enumerate(result.turns, start=1):
            console.print(f"\n[cyan]turn {n}[/cyan]")
            console.print(escape(turn.prompt))
            console.print(f"[cyan]reply {n}[/cyan]")
            console.print(escape(turn.response) or "[dim](empty)[/dim]")
    else:
        if case is not None:
            console.print("\n[cyan]prompt[/cyan]")
            console.print(case.prompt)

        console.print("\n[cyan]response[/cyan]")
        console.print(result.response or "[dim](empty)[/dim]")

    console.print("\n[cyan]tools called[/cyan]")
    if not result.tools:
        console.print(", ".join(result.tool_calls) or "[dim](none)[/dim]")
    for call in result.tools:
        console.print("  " + escape(calls_describe(call, turn=result.turns is not None)),
                      soft_wrap=True)
        if call.preview:
            console.print("    [dim]returned: " + escape(" ".join(call.preview.split())[:200])
                          + "[/dim]", soft_wrap=True)

    console.print("\n[cyan]checks[/cyan]")
    if result.error:
        console.print(f"  [red]error:[/red] {result.error}")
    for c in result.checks:
        mark = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
        console.print(f"  {mark} {c.kind}" + (f"  [dim]{c.detail}[/dim]" if c.detail else ""))
    if result.judge is not None:
        j = result.judge
        if j.error:
            console.print(f"  [yellow]judge error:[/yellow] {j.error}")
        else:
            mark = "[green]✓[/green]" if j.passed else "[red]✗[/red]"
            console.print(f"  {mark} judge {j.score}/10 (needs {j.threshold})  [dim]{j.model}[/dim]")
            if j.reasoning:
                console.print(f"      [dim]{j.reasoning}[/dim]")


app.add_typer(
    eval_app,
    name="eval",
    help="Run the hub's eval cases; inspect results and regressions.",
    rich_help_panel="Commands",
)


from .work_items.cli import operations_app, work_items_app  # noqa: E402

app.add_typer(work_items_app, name="work-items",
              help="Durable work items: start, watch, cancel and restart.",
              rich_help_panel="Commands")
app.add_typer(operations_app, name="operations",
              help="Recorded external writes: approve, deny and resolve.",
              rich_help_panel="Commands")

from .channels.cli import channels_app  # noqa: E402

app.add_typer(channels_app, name="channels",
              help="Channels: Slack, GitHub, Linear, email and WhatsApp threads for this hub.",
              rich_help_panel="Commands")

from .intake.cli import intake_app  # noqa: E402

app.add_typer(intake_app, name="intake",
              help="Intake: review email and WhatsApp messages before work starts.",
              rich_help_panel="Commands")

from .stations.cli import handoffs_app, stations_app  # noqa: E402

app.add_typer(stations_app, name="stations",
              help="Isolated agents (stations): validate them and inspect their calls.",
              rich_help_panel="Commands")
app.add_typer(handoffs_app, name="handoffs",
              help="Handoff documents between stations of a work item.",
              rich_help_panel="Commands")

from .budgets_cli import budgets_app  # noqa: E402
from .sandbox.cli import sandbox_app  # noqa: E402

app.add_typer(budgets_app, name="budgets",
              help="Usage limits: budgets.yaml, this month's usage and open reservations.",
              rich_help_panel="Commands")

app.add_typer(sandbox_app, name="sandbox",
              help="Sandboxes for agents: doctor, profiles, probes and cleanup.",
              rich_help_panel="Commands")

from .apps.cli import apps_app  # noqa: E402

app.add_typer(apps_app, name="apps",
              help="Cards: tool results shown as MCP Apps in chat, Claude and ChatGPT.",
              rich_help_panel="Commands")


if __name__ == "__main__":
    app()
