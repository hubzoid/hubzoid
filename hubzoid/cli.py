"""hubzoid CLI — typer app.

Commands:
  hubzoid init [PATH]              Scaffold a hub from the bundled template.
  hubzoid run [PATH]               Start a hub: bridge + web app on one port (Open WebUI in legacy mode).
  hubzoid gateway [HUBS...]        One shared Open WebUI fronting many hub bridges.
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
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
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


def _stop_processes(procs, *, timeout: float = 8.0) -> None:
    """Let child services close databases before the supervising process exits.

    In a container, exiting PID 1 kills its remaining children immediately.
    Signal all services first, then give them a shared, bounded grace period.
    """
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
    `--public-url`, then `HUBZOID_PUBLIC_URL`/`WEBUI_URL`, which OAuth already
    requires) - so behind a real TLS proxy the edge advertises https with zero
    extra setup, while a plain-http localhost run declares nothing and is left
    untouched. Returns "" when no https public URL is known.
    """
    candidates = [u for u in urls if u] + [
        env.get("HUBZOID_PUBLIC_URL", ""),
        env.get("WEBUI_URL", ""),
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
        "threshold breaches.",
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

    if fresh_hub:
        setup_dir = hub_dir / ".hubzoid"
        setup_dir.mkdir(exist_ok=True)
        (setup_dir / "fresh-install").touch(mode=0o600)

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
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
    port: int = typer.Option(None, "--port", help="Public port: the web app, downloads and MCP. Default: 3080 (or PORT env)."),
    bridge_port: int = typer.Option(None, "--bridge-port", help="FastAPI bridge port on 127.0.0.1. Default: 8000 (or BRIDGE_PORT env)."),
    host: str = typer.Option("127.0.0.1", "--host", envvar="HUBZOID_HOST", help="Interface the public port binds to (or HUBZOID_HOST env). 0.0.0.0 exposes it on the network, which needs sign-in (HUBZOID_AUTH=true). The bridge always stays on 127.0.0.1."),
    no_ui: bool = typer.Option(False, "--no-ui", help="Bridge only, on 127.0.0.1: no public port (and no Open WebUI in legacy mode)."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the web app in a browser. It opens only from a terminal, on a loopback host."),
    slack: bool = typer.Option(
        False,
        "--slack", "-s",
        help="Also start the Slack adapter (Socket Mode). Reads SLACK_BOT_TOKEN and "
        "SLACK_APP_TOKEN from .env. Soft-fails with a warning if either is missing.",
    ),
    whatsapp: bool = typer.Option(
        False,
        "--whatsapp",
        help="Also start the WhatsApp webhook surface. Reads WHATSAPP_* from .env and "
        "exposes /webhooks/whatsapp via the edge. Soft-skips if unconfigured.",
    ),
    telegram: bool = typer.Option(
        False,
        "--telegram",
        help="Also start the Telegram webhook surface. Reads TELEGRAM_* from .env and "
        "exposes /webhooks/<hub>/telegram via the edge. Soft-skips if unconfigured.",
    ),
    webhook: bool = typer.Option(
        False,
        "--webhook",
        help="Also start the generic webhook surface (alerting, CI, automations). "
        "Reads WEBHOOK_INBOUND_SECRET/_NAME/_HMAC from .env and exposes "
        "/webhooks/<hub>/<name> via the edge. Soft-skips if unconfigured.",
    ),
) -> None:
    """Start a hub: the bridge, and the Hubzoid web app on one public port.

    Sign-in is off by default (local mode: you are the hub's owner), so the
    public port stays on this machine. Turn sign-in on with HUBZOID_AUTH=true
    before binding another interface. HUBZOID_UI=openwebui runs the legacy Open
    WebUI chat app instead (pip install "hubzoid\\[openwebui]").
    """
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

    settings = _load_settings(hub)
    ui_port = port or settings.ui_port
    br_port = bridge_port or settings.bridge_port
    from . import appmode, config_secrets

    # A hub in a gateway's deployment runs as the gateway recorded (a bridge
    # started on its own, `gateway --no-bridges`, may not share its
    # environment). Settings that disagree stop it here, never silently.
    conflicts = appmode.deployment_conflicts(hub)
    if conflicts:
        for problem in conflicts:
            console.print(f"[red]{escape(problem)}[/red]")
        raise typer.Exit(2)

    # The web experience: the Hubzoid web app (default) or, for one release, the
    # legacy Open WebUI chat app. Read after the hub's .env is loaded.
    legacy = appmode.is_legacy(hub)
    auth_on = appmode.auth_enabled(hub)
    if legacy:
        if not no_ui:
            _require_openwebui()
    else:
        if not no_ui:
            _refuse_unauthenticated_network(hub, host, auth_on)
        _check_openwebui_upgrade(hub, auth_on)

    if legacy and not no_ui:
        os.environ["OWUI_INTERNAL_URL"] = f"http://127.0.0.1:{_owui_internal_port(ui_port) if os.environ.get('HUBZOID_DISABLE_EDGE', '').lower() not in ('1', 'true', 'yes') else ui_port}"

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
    if not legacy and not no_ui:
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
    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down...[/cyan]")
        # Unwind Popen.wait before waiting for children in finally. Waiting
        # inside the signal handler can re-enter Popen's non-reentrant lock.
        sys.exit(0)

    # Every child is stopped however run ends, including Ctrl-C or SIGTERM
    # while the others are still starting.
    children: list = []
    previous = (signal.signal(signal.SIGINT, _shutdown), signal.signal(signal.SIGTERM, _shutdown))
    try:
        console.print(f"[cyan]→ bridge[/cyan]  http://127.0.0.1:{br_port}  (hub: {hub.name})")
        bridge_proc = subprocess.Popen(bridge_cmd, env=bridge_env)
        children.append(bridge_proc)

        # 2. Wait for the bridge to come up before starting the public port.
        if not _wait_for_bridge(bridge_proc, f"http://127.0.0.1:{br_port}/healthz"):
            console.print("[red]bridge failed to come up[/red]")
            raise typer.Exit(1)
        console.print("[green]→ bridge[/green]  ready")

        if not no_ui and legacy:
            _start_openwebui(hub, settings, host=host, ui_port=ui_port, br_port=br_port,
                             inbound=bool(whatsapp or telegram or webhook), started=children)
        elif not no_ui:
            edge_proc = _start_web_app_edge(
                hub, settings, host=host, ui_port=ui_port, br_port=br_port,
                inbound=bool(whatsapp or telegram or webhook), started=children)
            if edge_proc is None:
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

        # Optional: spawn the Slack adapter as a third child. Soft-warn if the
        # operator asked for --slack but the .env is missing the tokens — the
        # bridge + UI keep running either way.
        if slack:
            from .slack.env import should_start_slack
            ok, warn = should_start_slack(want_slack=True, env=os.environ)
            if not ok:
                console.print(f"[yellow]→ slack [/yellow]  skipping: {warn}")
            else:
                slack_cmd = [sys.executable, "-m", "hubzoid", "slack", "run", str(hub)]
                children.append(subprocess.Popen(slack_cmd, env=bridge_env))
                console.print("[cyan]→ slack [/cyan]  starting (Socket Mode)")

        # Optional: the inbound surfaces (WhatsApp/Telegram/generic webhook) as one
        # shared child. It reads WHATSAPP_*/TELEGRAM_*/WEBHOOK_INBOUND_* from .env and
        # serves whichever are configured; the edge already routes /webhooks/<hub> to
        # it. Soft-warn per surface.
        if whatsapp or telegram or webhook:
            from .inbound.env import (
                missing_telegram_vars,
                missing_webhook_vars,
                missing_whatsapp_vars,
            )
            if whatsapp and missing_whatsapp_vars(os.environ):
                console.print(f"[yellow]→ inbound[/yellow]  whatsapp skipped: missing {', '.join(missing_whatsapp_vars(os.environ))}")
            if telegram and missing_telegram_vars(os.environ):
                console.print(f"[yellow]→ inbound[/yellow]  telegram skipped: missing {', '.join(missing_telegram_vars(os.environ))}")
            if webhook and missing_webhook_vars(os.environ):
                console.print(f"[yellow]→ inbound[/yellow]  webhook skipped: missing {', '.join(missing_webhook_vars(os.environ))}")
            start_wa = whatsapp and not missing_whatsapp_vars(os.environ)
            start_tg = telegram and not missing_telegram_vars(os.environ)
            start_wh = webhook and not missing_webhook_vars(os.environ)
            if start_wa or start_tg or start_wh:
                inbound_cmd = [sys.executable, "-m", "hubzoid", "inbound", "run", str(hub)]
                children.append(subprocess.Popen(inbound_cmd, env=bridge_env))
                surfaces = "+".join(s for s, on in (("whatsapp", start_wa), ("telegram", start_tg), ("webhook", start_wh)) if on)
                console.print(f"[cyan]→ inbound[/cyan]  starting ({surfaces}, /webhooks/<hub>)")

        # Block on the bridge process; its exit ends the CLI.
        bridge_proc.wait()
    finally:
        # The public side first, the bridge last (it holds the databases).
        _stop_processes(children[::-1])
        signal.signal(signal.SIGINT, previous[0])
        signal.signal(signal.SIGTERM, previous[1])


# ---------------------------------------------------------------------------
# run: helpers for the Hubzoid web app (default mode)
# ---------------------------------------------------------------------------
def _wait_for_bridge(proc, url: str, timeout: float = 180.0) -> bool:
    """Wait for the bridge's health check. Stops at once if the bridge exits,
    and is patient otherwise: a first start on a slow or busy machine creates
    databases and loads the agent runtime."""
    deadline = time.monotonic() + timeout
    noted = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        if _wait_for(url, timeout=5.0):
            return True
        if proc.poll() is not None:
            return False
        if not noted and time.monotonic() - started > 20:
            console.print("[dim]→ bridge  still starting (the first start creates the databases)[/dim]")
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


def _require_openwebui() -> None:
    """Legacy mode needs the `openwebui` extra. Checked before anything starts."""
    from . import webui

    if webui.is_available():
        return
    console.print(
        "[red]HUBZOID_UI=openwebui runs the legacy Open WebUI chat app, which is not installed.[/red]\n"
        "Install it (legacy mode, available for this release):\n"
        '  pip install "hubzoid\\[openwebui]"\n'
        "or remove HUBZOID_UI from the hub's .env to use the Hubzoid web app."
    )
    if _in_container():
        console.print("In Docker, build the image with --build-arg WITH_OPENWEBUI=true.")
    raise typer.Exit(1)


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
        "Hubzoid now has its own web app and sign-in. Choose one:\n"
        f"  1. Move accounts, groups and chats:  hubzoid migrate openwebui {quoted}\n"
        "     Do a dry run first (see `hubzoid migrate openwebui --help`), then back up\n"
        f"     (hubzoid backup {quoted}), stop the hub, and apply.\n"
        "  2. Keep Open WebUI for this release: pip install \"hubzoid\\[openwebui]\" and set\n"
        f"     HUBZOID_UI=openwebui in {escape(str(hub / '.env'))}"
    )
    raise typer.Exit(1)


def _start_web_app_edge(hub: Path, settings, *, host: str, ui_port: int, br_port: int,
                        inbound: bool, started: list):
    """The public port for the Hubzoid web app: every path goes to the bridge,
    `/webhooks/<hub>` to the inbound process. Returns the edge process (also
    added to `started`), or None when it could not start (the reason is printed)."""
    from . import appmode, config_secrets

    edge_env = config_secrets.deployment_view(os.environ)
    edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{br_port}"
    edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env)
    # The edge serves the web app, so it runs none of its Open WebUI rewrites.
    edge_env["HUBZOID_UI"] = appmode.UI_HUBZOID
    edge_routes = []
    if inbound:
        # Inbound surfaces receive on a loopback port; only /webhooks/<hub> is
        # exposed (each POST is signature-, secret- or HMAC-verified first).
        from .inbound.run import hub_slug, inbound_port

        edge_routes.append({"prefix": f"/webhooks/{hub_slug(hub, os.environ)}",
                            "upstream": f"http://127.0.0.1:{inbound_port(os.environ)}"})
    from .workflows.events import declarations
    from .inbound.run import hub_slug
    for name in declarations(hub):
        edge_routes.insert(0, {"prefix": f"/webhooks/{hub_slug(hub, os.environ)}/{name}",
                               "upstream": f"http://127.0.0.1:{br_port}"})
    edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(edge_routes)
    edge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.edge:_factory", "--factory",
        "--host", host, "--port", str(ui_port),
        "--log-level", settings.log_level,
    ]
    edge_proc = subprocess.Popen(edge_cmd, env=edge_env)
    started.append(edge_proc)
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host.strip("[]")
    probe = f"[{probe}]" if ":" in probe else probe
    ready = _wait_for(f"http://{probe}:{ui_port}/healthz", timeout=30)
    if edge_proc.poll() is not None:
        console.print(f"[red]The web app could not open port {ui_port} on {escape(host)}. Is another "
                      "program using it? Choose another with --port.[/red]")
        return None
    if not ready:
        console.print(f"[yellow]→ web app[/yellow]  not answering yet on port {ui_port}; "
                      "check the log above")
    return edge_proc


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


def _start_openwebui(hub: Path, settings, *, host: str, ui_port: int, br_port: int,
                     inbound: bool, started: list):
    """Legacy mode (HUBZOID_UI=openwebui): Open WebUI behind the edge, exactly as
    in 1.0.x. Each process is added to `started` as soon as it runs."""
    from . import config_secrets

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
            # The deployment layer only: never the hub secret or
            # restricted/.env (config_secrets.deployment_view).
            base_env=config_secrets.deployment_view(os.environ),
        )
        started.append(ui_proc)
        log_path = getattr(ui_proc, "_log_path", None)
        console.print("[cyan]→ webui [/cyan]  starting (Open WebUI; local embedding model is off, so boot is quick)")
        if log_path:
            console.print(f"            log: {log_path}")

        # Wait for OWUI on its (now possibly internal) bind before fronting it.
        owui_probe = "127.0.0.1" if owui_host in ("0.0.0.0", "::") else owui_host
        owui_ready = _wait_for(f"http://{owui_probe}:{owui_port}/", timeout=240)

        probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        display_url = f"http://{host}:{ui_port}"
        if edge_enabled:
            # Start the public-facing edge router in front of bridge + OWUI.
            edge_env = config_secrets.deployment_view(os.environ)
            edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{owui_port}"
            edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env)
            edge_routes = [
                {"prefix": "/artifacts", "upstream": f"http://127.0.0.1:{br_port}"},
                # The admin portal (SPA + JSON API) is served by the bridge;
                # expose it through the one public port like /artifacts.
                {"prefix": "/portal", "upstream": f"http://127.0.0.1:{br_port}"},
            ]
            if settings.mcp_server:
                # The hosted MCP surface is the one other bridge path that
                # is public by design (per-user OWUI api-key auth; /v1
                # stays loopback-only).
                edge_routes.append(
                    {"prefix": "/mcp", "upstream": f"http://127.0.0.1:{br_port}"}
                )
            if settings.mcp_server:
                for prefix in ("/.well-known/oauth-protected-resource/mcp",
                               "/.well-known/oauth-authorization-server/mcp/oauth"):
                    edge_routes.append({"prefix": prefix, "upstream": f"http://127.0.0.1:{br_port}"})
            if inbound:
                # Inbound surfaces receive on a loopback inbound port; only
                # /webhooks/<hub> is exposed publicly (each POST is signature-,
                # secret-, or HMAC-verified before anything runs). Namespaced by
                # hub slug so the same public path scheme works under the gateway.
                # Import here so a plain `hubzoid run` never pulls in the inbound
                # stack (SQLAlchemy, etc.).
                from .inbound.run import hub_slug, inbound_port
                edge_routes.append(
                    {"prefix": f"/webhooks/{hub_slug(hub, os.environ)}",
                     "upstream": f"http://127.0.0.1:{inbound_port(os.environ)}"}
                )
            edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(edge_routes)
            edge_cmd = [
                sys.executable, "-m", "uvicorn",
                "hubzoid.edge:_factory", "--factory",
                "--host", host, "--port", str(ui_port),
                "--log-level", settings.log_level,
            ]
            edge_paths = "/artifacts + /mcp" if settings.mcp_server else "/artifacts"
            console.print(f"[cyan]→ edge  [/cyan]  http://{host}:{ui_port}  ({edge_paths} → bridge :{br_port}, else → owui :{owui_port})")
            edge_proc = subprocess.Popen(edge_cmd, env=edge_env)
            started.append(edge_proc)
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


# ---------------------------------------------------------------------------
# gateway — one Open WebUI fronting many hub bridges
# ---------------------------------------------------------------------------
@app.command()
def gateway(
    hubs: list[Path] = typer.Argument(..., help="Hub directories to serve behind one front door."),
    port: int = typer.Option(None, "--port", help="Public port people reach the deployment on. Default: 3080 (or PORT env)."),
    host: str = typer.Option("127.0.0.1", "--host", envvar="HUBZOID_HOST", help="Interface the public edge binds to (or HUBZOID_HOST env). Use 0.0.0.0 to expose."),
    public_url: str = typer.Option(None, "--public-url", help="Public base URL (e.g. https://hub.example.com); used to build per-hub artifact download links. Falls back to HUBZOID_PUBLIC_URL."),
    name: str = typer.Option("Hubzoid", "--name", help="The deployment's display name (the shared Open WebUI's name in the legacy mode)."),
    data_dir: Path = typer.Option(None, "--data-dir", help="Gateway state dir: manifest, shared database, deployment key (and Open WebUI's data in the legacy mode). Default: ./.hubzoid-gateway."),
    launch_bridges: bool = typer.Option(True, "--launch-bridges/--no-bridges", help="Launch each hub's headless bridge. --no-bridges fronts bridges already running as separate units."),
) -> None:
    """Serve many hubs behind one front door — one headless bridge per hub.

    Hubzoid web app (the default): N bridges and one edge, no Open WebUI. Every
    bridge shares the operational store (accounts, sessions, conversations,
    groups, access), so one sign-in covers every agent the person may use. The
    edge sends /b/<slug>/api, /artifacts, /mcp and /branding to that hub's
    bridge and everything else to the first bridge. The page chrome comes from
    HUBZOID_GATEWAY_BRANDING (a hub slug or a path), <data-dir>/branding/, or
    the first hub's branding/. Sign-in is set once, in the gateway's
    environment (HUBZOID_AUTH); a hub .env that disagrees stops the gateway.

    Legacy Open WebUI mode (HUBZOID_UI=openwebui), as in 1.0.x: ONE Open WebUI
    over many hubs. Lighter than one `hubzoid run` per hub (a single OWUI
    process instead of N). Each hub surfaces as a selectable model. With
    HUBZOID_GATEWAY_ADMIN_EMAIL/PASSWORD set, each hub's model entry (name,
    description, suggestions, avatar) and team group + read ACL are
    provisioned automatically on boot; admins then only add users to groups.
    Gateway chrome branding (favicon, splash, sidebar) comes from the first
    hub's branding/ by default — override with HUBZOID_GATEWAY_BRANDING
    (a hub slug or a path), or populate <data-dir>/branding/. Artifact
    downloads route per hub via `/b/<slug>/artifacts`. See docs/DEPLOYING.md.
    """
    from . import gateway as gateway_lib
    from . import webui

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
        for_owui_only = sorted(k for k in dep_values if not config_secrets.bridge_deployment_key(k))
        if for_owui_only:
            console.print(f"[yellow]→ secrets[/yellow]  kept from bridges (gateway and Open WebUI only): "
                          f"{', '.join(for_owui_only)}")
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

    # The shared OWUI and all bridges use deployment-wide storage. plan() loads
    # individual hub .env files; the last hub must not redirect the shared DB.
    for key in ("DATABASE_URL", "DATABASE_SCHEMA"):
        if key in deployment_env:
            os.environ[key] = deployment_env[key]
        else:
            os.environ.pop(key, None)

    # Sign-in and Open WebUI settings kept in hub .env files still apply to the
    # shared UI when the gateway's environment does not set them (0.9.x compat).
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
    gw_data = (data_dir or (Path.cwd() / ".hubzoid-gateway")).resolve()
    log_level = os.environ.get("HUB_LOG_LEVEL", "info")
    # One shared operational DB for the whole gateway (access grants, per-hub
    # authority markers, identities, audit, workflow catalog): a local URL passed
    # to every bridge, so an org grant in hub A is visible in hub B and one bridge
    # can serve the org-wide portal. DBOS system tables stay per-bridge
    # (db.dbos_url). An operator's HUBZOID_OPERATIONAL_DB / DATABASE_URL wins.
    _op_override = deployment_env.get("HUBZOID_OPERATIONAL_DB") or deployment_env.get("DATABASE_URL")
    if not _op_override:
        gw_data.mkdir(parents=True, exist_ok=True)
    shared_op_url = _op_override or f"sqlite:///{gw_data / 'hubzoid-operational.db'}"

    from . import appmode
    if not appmode.is_legacy(env=os.environ):
        _gateway_web_app(
            gp=gp, hub_dirs=hub_dirs, deployment_env=deployment_env, process_env=process_env,
            dep_secret=dep_secret, dep_values=dep_values, conflicts=conflicts, host=host,
            ui_port=ui_port, pub=pub, gw_data=gw_data, log_level=log_level,
            shared_op_url=shared_op_url, name=name, launch_bridges=launch_bridges)
        return

    from . import deployment
    # Hiding Open WebUI's user management is the normal setup for a gateway set
    # up fresh with Console accounts (sign-in on, a service account): recorded
    # once in the manifest and kept. An existing deployment records nothing and
    # keeps Open WebUI's Users page. HUBZOID_HIDE_OWUI_USERS overrides both.
    try:
        _prior = json.loads((gw_data / "deployment.json").read_text())
    except (OSError, ValueError):
        _prior = {}
    _hide_default = deployment.hide_owui_users_default(
        _prior, fresh=not (gw_data / "webui.db").exists(), env=os.environ)
    if len(hub_dirs) > 1 and deployment_env.get('HUBZOID_DBOS_DB','').startswith('sqlite'):
        console.print('[red]A multi-hub gateway requires separate SQLite DBOS files. Unset HUBZOID_DBOS_DB or use PostgreSQL.[/red]')
        raise typer.Exit(2)
    deployment.save(gw_data / "deployment.json",
        hubs=[dict(key=b.hub_dir.name.lower(), name=b.display_name or b.slug,
                   path=str(b.hub_dir), model_id=b.model_label, slug=b.slug,
                   dbos_url=deployment_env.get('HUBZOID_DBOS_DB') or
                       (deployment_env.get('DATABASE_URL') if deployment_env.get('DATABASE_URL','').startswith('postgres') else f"sqlite:///{b.hub_dir}/.hubzoid/dbos.db")) for b in gp.backends],
        operational_url=shared_op_url,
        owui_url=f"http://127.0.0.1:{_owui_internal_port(ui_port) if os.environ.get('HUBZOID_DISABLE_EDGE', '').lower() not in ('1', 'true', 'yes') else ui_port}",
        owui_db=str(gw_data / "webui.db"),
        owui_database_url=deployment_env.get("DATABASE_URL") or f"sqlite:///{gw_data / 'webui.db'}",
        owui_database_schema=deployment_env.get("DATABASE_SCHEMA"),
        deployment_secret=dep_secret,
        owner=os.environ.get("HUBZOID_GATEWAY_ADMIN_EMAIL"),
        public_url=pub or os.environ.get("WEBUI_URL"),
        workflow_user=os.environ.get("HUBZOID_WORKFLOW_USER"),
        hide_owui_users=_hide_default,
        # How the shared chat app signs people in (flags only, no values), so
        # the Console on every bridge offers only sign-in modes that work.
        sign_in=deployment.sign_in_flags(os.environ),
        # The mode, for bridges started separately (gateway --no-bridges).
        ui_mode=appmode.UI_OPENWEBUI, auth=appmode.auth_enabled(env=os.environ),
        allowed_origins=_deployment_origins(pub))
    _explicit_hide = (os.environ.get("HUBZOID_HIDE_OWUI_USERS") or "").strip()
    if _explicit_hide or _hide_default:
        _hidden = (_explicit_hide.lower() in ("1", "true", "yes", "on")) if _explicit_hide else True
        console.print(
            "[cyan]→ accounts[/cyan]  Open WebUI's Users page is "
            + ("hidden: manage accounts in the Admin Console (People)" if _hidden else "shown")
            + (" (HUBZOID_HIDE_OWUI_USERS)" if _explicit_hide
               else "; set HUBZOID_HIDE_OWUI_USERS=false to show it"))

    # Deterministic gateway chrome branding. Stamp a chosen logo / favicon into
    # OWUI's static dirs so the login page, tab icon and sidebar show a brand
    # mark, not whatever a prior single-hub `run` last left in the shared OWUI
    # install. Source resolves (see GatewayPlan.branding_source):
    #   HUBZOID_GATEWAY_BRANDING (hub slug or path) -> <data_dir>/branding when
    #   populated -> the first hub's branding/ -> baseline CSS only.
    # Uses the GATEWAY baseline CSS, which keeps the Workspace nav visible
    # (gateway admins manage per-team Groups + per-model ACLs there) — unlike
    # the single-hub baseline, which hides it. Per-hub identity belongs on the
    # model avatar, not here: one shared chrome fronts N hubs, so the global
    # logo is org-level by design. Best-effort: a read-only OWUI install
    # (root-owned site-packages) must not stop the gateway from booting.
    from . import branding
    brand_src = gp.branding_source(gw_data, os.environ.get("HUBZOID_GATEWAY_BRANDING"))
    try:
        static_roots = branding.static_dirs()
        for sd in static_roots:
            branding.apply(brand_src, sd, baseline_css=branding.GATEWAY_BASELINE_CSS)
        if static_roots:
            console.print(f"[cyan]→ branding[/cyan]  chrome from {brand_src}/branding")
        else:
            console.print("[yellow]→ branding[/yellow]  Open WebUI static dirs not found; default look kept")
    except OSError as exc:
        console.print(f"[yellow]→ branding[/yellow]  skipped ({exc}); default look kept")

    # Whether this boot starts from a fresh OWUI state dir. Decided BEFORE
    # OWUI runs (it creates webui.db on boot): only a truly fresh dir may
    # bootstrap the first admin account during provisioning below.
    fresh_owui_db = not (gw_data / "webui.db").exists()

    procs: list[subprocess.Popen] = []

    # Native MCP is gateway-wide: the shared OWUI holds one tool-server registry
    # and one token store, so it is on for the whole gateway or off. Resolve once
    # and pin the shared OWUI + every bridge to that single value, so a hub .env
    # the plan loop loaded last cannot make it per-bridge-inconsistent.
    native_mcp = os.environ.get("OWUI_NATIVE_MCP", "").strip().lower() in ("1", "true", "yes", "on")
    os.environ["OWUI_NATIVE_MCP"] = "true" if native_mcp else "false"

    # 1. Launch each hub's headless bridge (unless they already run elsewhere).
    if launch_bridges:
        for b in gp.backends:
            bridge_env = os.environ.copy()
            if dep_secret:
                # A bridge takes only BRIDGE_DEPLOYMENT_KEYS from the deployment
                # secret. It must not fetch the secret again: the pins below
                # (per-hub public URL and so on) would be overwritten.
                for key in dep_values:
                    if config_secrets.bridge_deployment_key(key):
                        continue
                    if key in process_env:
                        bridge_env[key] = process_env[key]
                    else:
                        bridge_env.pop(key, None)
                bridge_env[config_secrets.INHERITED_MARKER] = "1"
            # Shared OWUI data directory for uploads and legacy SQLite readers.
            # Identity readers use the manifest's database URL (Postgres or
            # SQLite); keep this path even when DATABASE_URL selects Postgres.
            bridge_env["HUBZOID_OWUI_DB"] = str(gw_data / "webui.db")
            # Per-hub public base so this bridge's artifact links resolve
            # through the edge back to itself. Only injected when the hub's
            # own .env doesn't already pin HUBZOID_PUBLIC_URL.
            if pub:
                bridge_env["HUBZOID_PUBLIC_URL"] = gp.public_url_for(pub, b)
            # Pin the MCP flags per hub. plan() read each hub's own .env
            # file; the plan loop also loaded every .env into THIS process's
            # env (override=True), so values left behind by hub A would
            # otherwise leak into hub B's bridge via os.environ.copy(). The
            # hub's own .env still wins inside the bridge (settings.load
            # overrides), so this only settles the .env-less inheritance.
            bridge_env["MCP_SERVER"] = "true" if b.mcp else "false"
            bridge_env["MCP_ACCESS_GROUP"] = b.mcp_access_group
            bridge_env["MCP_PUBLIC_URL"] = b.mcp_public_url
            # Gateway mode: enable scheduled workflows (the HUBZOID_SCHEDULES gate
            # is auto-satisfied here), and pin every bridge to ONE shared
            # operational DB (access grants, per-hub authority markers, identities,
            # audit, workflow catalog) so an org grant in hub A is visible in hub
            # B and one bridge can serve the org-wide portal. DBOS system tables
            # stay per-bridge (db.dbos_url), so no shared-SQLite DBOS topology.
            # An operator's explicit HUBZOID_OPERATIONAL_DB / DATABASE_URL wins.
            bridge_env["HUBZOID_GATEWAY"] = "1"
            bridge_env["HUBZOID_OPERATIONAL_DB"] = shared_op_url
            # Gateway-wide native MCP (resolved above) - pin every bridge to it.
            bridge_env["OWUI_NATIVE_MCP"] = "true" if native_mcp else "false"
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
            brand_dir=brand_src,
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

    # 3b. Per-hub provisioning (opt-in). With admin credentials in the env,
    # seed each hub's OWUI model entry (picker name, description, suggestions,
    # avatar) and its team group + read ACL, so a new hub works for its team
    # on next boot with no manual admin steps. Without credentials this is
    # skipped entirely. Fail-safe: any error logs and the gateway boots on.
    # See hubzoid/gateway_provision.py for the overwrite policy.
    admin_email = os.environ.get("HUBZOID_GATEWAY_ADMIN_EMAIL", "").strip()
    admin_password = os.environ.get("HUBZOID_GATEWAY_ADMIN_PASSWORD", "").strip()
    auth_on = os.environ.get("WEBUI_AUTH", "").strip().lower() in ("true", "1", "yes", "on")
    if admin_email and admin_password:
        if not auth_on:
            # HARD prerequisite: with WEBUI_AUTH off, OWUI's signin ignores
            # credentials and mints the well-known admin@localhost/'admin'
            # account — a booby trap the moment auth is later enabled. Never
            # provision in that mode.
            console.print(
                "[yellow]→ provision[/yellow]  skipped: provisioning needs "
                "WEBUI_AUTH=true (see docs/auth.md); with auth off, Open WebUI "
                "would create the default admin@localhost account instead of yours"
            )
        elif not owui_ready:
            console.print(
                "[yellow]→ provision[/yellow]  skipped: Open WebUI is not ready; "
                "will run on next boot"
            )
        else:
            from . import gateway_provision as gwp
            specs = [
                gwp.HubSpec(
                    model_id=b.model_label,
                    name=b.display_name or b.slug,
                    group=b.slug,
                    suggestions=b.suggestions,
                    description=b.description,
                    logo=b.logo,
                )
                for b in gp.backends
            ]
            try:
                actions = gwp.provision(
                    base_url=f"http://{owui_probe}:{owui_port}",
                    email=admin_email,
                    password=admin_password,
                    hubs=specs,
                    allow_bootstrap=fresh_owui_db,
                )
                for a in actions:
                    console.print(f"[cyan]→ provision[/cyan]  {a}")
            except Exception as exc:  # noqa: BLE001 — provisioning never kills the boot
                console.print(f"[yellow]→ provision[/yellow]  skipped: {exc}")
    elif admin_email or admin_password:
        console.print(
            "[yellow]→ provision[/yellow]  skipped: set BOTH "
            "HUBZOID_GATEWAY_ADMIN_EMAIL and HUBZOID_GATEWAY_ADMIN_PASSWORD"
        )

    # 4. The public edge: per-hub artifact prefixes -> bridges, rest -> OWUI.
    edge_proc = None
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    display_url = f"http://{host}:{ui_port}"
    if edge_enabled:
        edge_env = os.environ.copy()
        edge_env["HUBZOID_EDGE_DEFAULT"] = f"http://127.0.0.1:{owui_port}"
        edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env, pub)
        gw_routes = list(gp.edge_routes())
        # Any bridge can serve the org-wide portal (they share one database).
        # The first answers; the others take over while it is down or restarting.
        if gp.backends:
            bridges = [f"http://127.0.0.1:{b.bridge_port}" for b in gp.backends]
            gw_routes.append({"prefix": "/portal", "upstream": bridges[0], "fallbacks": bridges[1:]})
        edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(gw_routes)
        edge_env["HUBZOID_DEPLOYMENT"] = str(gw_data / "deployment.json")
        # The edge locks only migrated model ACLs dynamically. Do not lock
        # shared group management during a partial migration.
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
        console.print(f"[green]→ gateway[/green]  ready    {display_url}" if owui_ready else "[yellow]→ gateway[/yellow]  OWUI not ready; check logs.")

    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down gateway...[/cyan]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # Block on the shared OWUI; its exit ends the gateway.
    try:
        owui_proc.wait()
    finally:
        _stop_processes(reversed(procs))


def _gateway_mode_conflicts(hub_dirs: list[Path], *, auth_on: bool, env, conflicts: dict) -> list[str]:
    """Hub settings that would make one bridge of a web app gateway behave
    differently from the rest. Each bridge loads its own hub .env over the
    gateway's environment, so a hub that turns sign-in off, switches to Open
    WebUI or brings its own deployment key would split the deployment (one
    bridge without sign-in behind a public edge serves everyone as the local
    owner). Refused, never guessed."""
    from . import gateway as gateway_lib

    problems: list[str] = []
    for h in hub_dirs:
        own = gateway_lib._own_env(h)
        ui = (own.get("HUBZOID_UI") or "").strip()
        if ui and _is_legacy_value(ui):
            problems.append(
                f"{h.name}/.env sets HUBZOID_UI={ui}, but this gateway runs the Hubzoid web app. "
                "Remove it, or set HUBZOID_UI=openwebui in the gateway's environment for every hub.")
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
    decided = (env.get("HUBZOID_AUTH") or "").strip()
    if not decided and "WEBUI_AUTH" in conflicts:
        problems.append(
            f"Hubs disagree on WEBUI_AUTH ({', '.join(conflicts['WEBUI_AUTH'])}). Set HUBZOID_AUTH "
            "in the gateway's environment so every agent has the same sign-in.")
    return problems


def _deployment_origins(pub: str) -> list[str]:
    """Every browser origin of the deployment: the public URL (`--public-url`
    or the environment's) plus HUBZOID_ALLOWED_ORIGINS."""
    from . import appmode

    env = dict(os.environ)
    if pub:
        env["HUBZOID_PUBLIC_URL"] = pub
    return appmode.allowed_origins(env)


def _is_legacy_value(raw: str) -> bool:
    from . import appmode

    return appmode.is_legacy(env={"HUBZOID_UI": raw})


def _gateway_upgrade_guard(gw_data: Path, prior: dict, operational_url: str, *, auth_on: bool) -> None:
    """A gateway that ran Open WebUI before (1.0.x) keeps its people and chats
    there until they are moved. With sign-in on and no Hubzoid account yet,
    starting the web app would lock everyone out: stop with instructions. With
    sign-in off, say that old chats can be imported.

    The local owner that sign-in-off mode creates (admin@localhost) is not an
    account anyone signs in to, so it does not count. A start in the web app
    keeps the manifest's record of Open WebUI's database (see
    _gateway_web_app), so a database server recorded there still counts as
    Open WebUI data after a start in local mode."""
    server_db = str(prior.get("owui_database_url") or "")
    had_owui = (gw_data / "webui.db").exists() or (
        bool(prior) and str(prior.get("ui_mode") or "openwebui") != "hubzoid"
        and bool(prior.get("owui_url"))) or (bool(server_db) and not server_db.startswith("sqlite"))
    if not had_owui:
        return
    from . import db as dblib
    from . import migrations, upgrade

    try:
        engine = dblib._engine_for_url(operational_url)
        migrations.upgrade(engine, "operational")
        with engine.connect() as conn:
            accounts = upgrade.people(conn)
    except Exception as exc:  # noqa: BLE001 — fail closed: never start blind
        console.print(f"[red]Could not read the Hubzoid accounts ({type(exc).__name__}). "
                      "Check the operational database, then start the gateway again.[/red]")
        raise typer.Exit(1)
    if accounts:
        return
    if auth_on:
        console.print(
            "[red]This gateway ran Open WebUI before, and no one has a Hubzoid account yet, "
            "so nobody could sign in.[/red]\n"
            "  Move its people and chats first:  hubzoid migrate openwebui   "
            "(see hubzoid migrate --help)\n"
            "  Or keep Open WebUI for this release: set HUBZOID_UI=openwebui")
        raise typer.Exit(1)
    console.print("[yellow]→ upgrade[/yellow]  Open WebUI data found here. Old chats can be "
                  "imported into the Hubzoid web app with `hubzoid migrate openwebui`.")


def _wait_any(procs: list, *, interval: float = 0.5) -> None:
    """Block until any child process exits."""
    while all(p.poll() is None for p in procs):
        time.sleep(interval)


# How long a web app gateway waits for each bridge (imports, migrations and the
# workflow engine; slower on a busy host).
_GATEWAY_BRIDGE_TIMEOUT = 180.0


def _stop_groups(procs, *, timeout: float = 15.0) -> None:
    """Stop child services and everything they started. Each was started in its
    own session (process group), so a bridge's server process stops with the
    `hubzoid run` that supervises it, even if that one is killed mid-start."""
    killpg = getattr(os, "killpg", None)

    def signal_group(p, sig) -> None:
        try:
            if killpg is not None:
                killpg(p.pid, sig)
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
    """`hubzoid gateway` in the Hubzoid web app mode: N bridges and one edge,
    no Open WebUI and no Open WebUI provisioning.

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
                  ("HUBZOID_ADMIN_EMAIL", "HUBZOID_GATEWAY_ADMIN_EMAIL", "WEBUI_ADMIN_EMAIL")
                  if os.environ.get(k, "").strip()), None)
    deployment.save(
        gw_data / "deployment.json",
        hubs=[dict(key=b.hub_dir.name.lower(), name=b.display_name or b.slug,
                   path=str(b.hub_dir), model_id=b.model_label, slug=b.slug,
                   dbos_url=deployment_env.get('HUBZOID_DBOS_DB') or
                       (deployment_env.get('DATABASE_URL') if deployment_env.get('DATABASE_URL', '').startswith('postgres')
                        else f"sqlite:///{b.hub_dir}/.hubzoid/dbos.db")) for b in gp.backends],
        operational_url=shared_op_url,
        # No Open WebUI runs in this mode. A gateway that ran it before keeps
        # where its database is: `hubzoid migrate openwebui` and the upgrade
        # guard find the people and chats still to move there.
        owui_url="", owui_db=str(prior.get("owui_db") or ""),
        owui_database_url=prior.get("owui_database_url") or None,
        owui_database_schema=prior.get("owui_database_schema") or None,
        deployment_secret=dep_secret,
        owner=owner,
        public_url=pub or os.environ.get("WEBUI_URL"),
        workflow_user=os.environ.get("HUBZOID_WORKFLOW_USER"),
        sign_in=deployment.sign_in_flags(os.environ),
        ui_mode=appmode.UI_HUBZOID, auth=auth_on,
        allowed_origins=_deployment_origins(pub),
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
    bridges = [f"http://127.0.0.1:{b.bridge_port}" for b in gp.backends]
    try:
        _gateway_web_app_serve(gp=gp, procs=procs, bridges=bridges, process_env=process_env,
                               dep_secret=dep_secret, dep_values=dep_values, auth_on=auth_on,
                               host=host, ui_port=ui_port, pub=pub, gw_data=gw_data,
                               log_level=log_level, shared_op_url=shared_op_url,
                               launch_bridges=launch_bridges)
    finally:
        _stop_groups(reversed(procs))


def _gateway_web_app_serve(*, gp, procs, bridges, process_env, dep_secret, dep_values, auth_on,
                           host, ui_port, pub, gw_data, log_level, shared_op_url,
                           launch_bridges) -> None:
    """Start the bridges and the edge (recorded in `procs`, which the caller
    stops), then block until one of them exits."""
    from . import appmode, config_secrets

    if launch_bridges:
        for b in gp.backends:
            bridge_env = os.environ.copy()
            if dep_secret:
                # As in the legacy gateway: a bridge takes only
                # BRIDGE_DEPLOYMENT_KEYS from the deployment secret and never
                # fetches it again.
                for key in dep_values:
                    if config_secrets.bridge_deployment_key(key):
                        continue
                    if key in process_env:
                        bridge_env[key] = process_env[key]
                    else:
                        bridge_env.pop(key, None)
                bridge_env[config_secrets.INHERITED_MARKER] = "1"
            bridge_env.pop("HUBZOID_OWUI_DB", None)
            if pub:
                bridge_env["HUBZOID_PUBLIC_URL"] = gp.public_url_for(pub, b)
            bridge_env["MCP_SERVER"] = "true" if b.mcp else "false"
            bridge_env["MCP_ACCESS_GROUP"] = b.mcp_access_group
            bridge_env["MCP_PUBLIC_URL"] = b.mcp_public_url
            bridge_env["HUBZOID_GATEWAY"] = "1"
            bridge_env["HUBZOID_OPERATIONAL_DB"] = shared_op_url
            # One mode and one sign-in setting for the whole deployment
            # (_gateway_mode_conflicts refused hubs that would differ).
            bridge_env["HUBZOID_UI"] = appmode.UI_HUBZOID
            bridge_env["HUBZOID_AUTH"] = "true" if auth_on else "false"
            # Bridges bind loopback only; the edge is the public door.
            bridge_env["HUBZOID_HOST"] = "127.0.0.1"
            cmd = [
                sys.executable, "-m", "hubzoid", "run", str(b.hub_dir),
                "--no-ui", "--bridge-port", str(b.bridge_port),
            ]
            procs.append(subprocess.Popen(cmd, env=bridge_env, start_new_session=True))
            console.print(f"[cyan]→ bridge[/cyan]  {b.slug}  http://127.0.0.1:{b.bridge_port}")

    for b in gp.backends:
        if not _wait_for(f"http://127.0.0.1:{b.bridge_port}/healthz", timeout=_GATEWAY_BRIDGE_TIMEOUT):
            console.print(f"[red]bridge {b.slug} (:{b.bridge_port}) failed to come up[/red]")
            raise typer.Exit(1)
    console.print(f"[green]→ bridges[/green]  {len(gp.backends)} ready: {', '.join(b.slug for b in gp.backends)}")

    edge_env = os.environ.copy()
    edge_env["HUBZOID_UI"] = appmode.UI_HUBZOID
    edge_env["HUBZOID_EDGE_DEFAULT"] = bridges[0]
    edge_env["HUBZOID_EDGE_DEFAULT_FALLBACKS"] = json.dumps(bridges[1:])
    edge_env["HUBZOID_EDGE_PUBLIC_SCHEME"] = _public_scheme(edge_env, pub)
    edge_env["HUBZOID_EDGE_ROUTES"] = json.dumps(gp.edge_routes(web_app=True))
    edge_env["HUBZOID_DEPLOYMENT"] = str(gw_data / "deployment.json")
    edge_cmd = [
        sys.executable, "-m", "uvicorn",
        "hubzoid.edge:_factory", "--factory",
        "--host", host, "--port", str(ui_port),
        "--log-level", log_level,
    ]
    edge_proc = subprocess.Popen(edge_cmd, env=edge_env, start_new_session=True)
    procs.append(edge_proc)
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    display_url = f"http://{host}:{ui_port}"
    if _wait_for(f"http://{probe_host}:{ui_port}/healthz", timeout=30) and edge_proc.poll() is None:
        console.print(f"[green]→ gateway[/green]  ready    {display_url}")
    else:
        console.print(f"[yellow]→ gateway[/yellow]  not fully ready; check logs. URL: {display_url}")

    def _shutdown(signum, frame):  # noqa: ARG001
        console.print("\n[cyan]shutting down gateway...[/cyan]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    # Any child exiting ends the gateway: a deployment with a dead bridge or
    # edge must be restarted by its supervisor, not limp on.
    _wait_any(procs)


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------
@app.command("backup")
def backup_cmd(
    hub: Path = typer.Argument(Path("."), help="A hub directory. A hub in a gateway backs up the whole gateway."),
    out: Path = typer.Option(None, "--out", "-o", help="Archive path. Default: ./hubzoid-backup-<time>.tar.gz"),
    include_secrets: bool = typer.Option(False, "--include-secrets", help="Also save .env files and signing keys."),
    wait: int = typer.Option(600, "--wait", help="Seconds to wait for running scheduled work. 0 = do not wait."),
) -> None:
    """Save a deployment's state to one archive while it keeps serving chat.

    New scheduled runs are held and running ones finish first. Hub content
    (AGENTS.md, skills, knowledge) belongs in git and is not included, nor are
    PostgreSQL databases (see docs/BACKUP.md)."""
    from datetime import datetime as _dt

    from . import backup as backup_lib

    out = out or Path.cwd() / f"hubzoid-backup-{_dt.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    try:
        index = backup_lib.backup(hub.resolve(), out, include_secrets=include_secrets,
                                  wait=wait, actor=_operator(), say=console.print)
    except (backup_lib.BackupError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Backup written:[/green] {out.resolve()} "
                  f"({len(index['roots'])} locations, {len(index['sqlite'])} databases)")
    if any(r["kind"] == "ui" for r in index["roots"]):
        console.print("The archive holds user accounts, chats and the chat UI's connection settings. "
                      "Store it like a secret.")
    else:
        # A hub behind a gateway that has not started on this release yet has no
        # pointer to the gateway, so only this hub's state was found.
        console.print("[yellow]No chat app data was found, so this archive has no user accounts "
                      "or chats.[/yellow] A hub behind a gateway is covered only after the gateway "
                      "has started on this version. Before that, also archive the gateway's "
                      "--data-dir (see docs/UPGRADING.md). Store the archive like a secret.")
    if not include_secrets:
        console.print("Left out: .env files, signing keys and database passwords. "
                      "Keep a copy of each .env elsewhere.")
    for url in index["not_included"]:
        console.print(f"[yellow]Not included (PostgreSQL):[/yellow] {url}. Back it up with pg_dump.")


@app.command("restore")
def restore_cmd(
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, help="An archive from hubzoid backup."),
    move: list[str] = typer.Option([], "--move", help="OLD=NEW: restore paths under OLD to NEW instead. Repeatable."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show where everything would go and stop."),
) -> None:
    """Put a backup back. Stop the hub or gateway first.

    Everything returns to its original path unless moved with --move. What is
    at a target now is kept beside it as <name>.pre-restore-<time>."""
    from . import backup as backup_lib

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
        result = backup_lib.restore(archive, moves, say=console.print)
    except backup_lib.BackupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    for aside in result["kept"]:
        console.print(f"Previous state kept at {aside}")
    for url in result["not_included"]:
        console.print(f"[yellow]Not in this archive (PostgreSQL):[/yellow] {url}. Restore it with pg_restore.")
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
    and scheduled work. Read only. Exits 1 when any check fails."""
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
                 "fail": "[red]✗[/red]"}
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
    typer.echo(systemd_unit_for_hub(hub_dir=hub, python_path=python_path, user=user))


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


@schedule_app.command("run")
def schedule_run(
    hub: Path = typer.Argument(..., help="Hub directory."),
    task_name: str = typer.Argument(..., metavar="TASK", help="Task name (the md filename stem)."),
    timeout: int = typer.Option(None, "--timeout", help="Override the task's per-round timeout (seconds)."),
    max_rounds: int = typer.Option(None, "--max-rounds", help="Override the task's round cap."),
    model: str = typer.Option(None, "--model", help="Override the model for this run (LLM tasks), e.g. claude-local/opus."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the round-1 prompt (LLM) or the command (script) and exit."),
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
        if dry_run:
            if match_def is None:
                console.print(f"[red]No workflow {task_name!r} found[/red]")
                raise typer.Exit(2)
            console.print(f"Would run workflow {want} from {match_def['source']}; no code executed.")
            return
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
            live = enqueue_live(hub, want)
            if live is not None:
                _audit_cli_start(hub, want, live.get_workflow_id())
                console.print(f"Workflow {want}: {live.get_result()!r}")
                return
            _wf.init(hub)
            _wf.load_workflows(hub)
            _wf.launch()
            wf_names = {w.name for w in _wf.registry()}
            want = task_name.replace("-", "_")
            match = next((n for n in wf_names if n == task_name or n == want), None)
            if match:
                console.print(f"[cyan]→ running workflow {match}[/cyan]")
                try:
                    handle = _wf.start(match)
                    _audit_cli_start(hub, match, handle.get_workflow_id())
                    result = handle.get_result()
                except Exception as run_exc:  # noqa: BLE001 — the run failed, not the load
                    console.print(f"[red]✗ workflow {match} failed: {type(run_exc).__name__}: {run_exc}[/red]")
                    console.print("[dim]The failed run is in `hubzoid schedule status` and the Console's Runs page.[/dim]")
                    raise typer.Exit(1)
                finally:
                    _wf.shutdown()
                console.print(f"[green]✓ workflow {match} returned:[/green] {result!r}")
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
        _wf.launch()
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
) -> None:
    """Show each task's recorded fire history (anchors, last result, last log)."""
    from . import scheduling as sch

    hub = hub.resolve()
    tasks, _ = sch.load_tasks(hub)
    state = sch.ScheduleState(hub)
    from .workflows.observe import catalog, runs
    workflows = catalog(hub)
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
) -> None:
    """Show every permission a subject effectively holds in a hub."""
    from .access import store_for

    domain = hub or hub_dir.resolve().name
    perms = sorted(store_for(hub_dir).permissions_for(subject, domain))
    if not perms:
        console.print(f"[dim]{subject} has no access in {domain}[/dim]")
        return
    console.print(f"{subject} in [bold]{domain}[/bold]: " + ", ".join(perms))


@access_app.command("list")
def access_list(
    hub: str = typer.Option(None, "--hub", help="Only this hub (Casbin domain)."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """List all grants (subject · permission · hub)."""
    from .access import store_for

    rows = store_for(hub_dir).list_grants(hub)
    if not rows:
        console.print("[dim]no grants[/dim]")
        return
    for subject, dom, perm in rows:
        console.print(f"{subject:30.30}  {perm:20.20}  [dim]{dom}[/dim]")
    console.print(f"[dim]{len(rows)} grant(s)[/dim]")


@access_app.command("bootstrap")
def access_bootstrap(
    admin: list[str] = typer.Option([], "--admin", help="Subject to make an org admin (repeatable)."),
    authoritative: bool = typer.Option(
        False, "--authoritative",
        help="Make Casbin the authority now (fresh install, no legacy to migrate).",
    ),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """First-boot bootstrap: grant org admins once (idempotent), optionally make
    Casbin authoritative. Break-glass for a fresh install."""
    from .access import store_for

    store_for(hub_dir).bootstrap(admin, authoritative=authoritative, hub=hub_dir.resolve().name)
    console.print(
        f"[green]bootstrapped[/green] admins={list(admin) or '(none)'} "
        f"authoritative={authoritative}"
    )


def _build_plan(hub_dir: Path, from_owui: str | None, model_id: str | None, standalone_public: bool = False):
    from .access import migrate

    hub_name = hub_dir.resolve().name
    plan = migrate.plan_from_csv(hub_dir, hub_name)
    if standalone_public:
        from .deployment import read
        if read(hub_dir):
            raise migrate.MigrationBlocked('registered gateways require OWUI model evidence')
        if not from_owui:
            from .access.owui_db import db_path
            local_owui = db_path(hub_dir)
            if local_owui.is_file():
                from_owui = f'sqlite:///{local_owui.resolve()}'
            else:
                return migrate.plan_standalone_public(hub_dir, plan)
    if from_owui:
        from sqlalchemy import create_engine

        from .deployment import permission_catalog, read
        registered = read(hub_dir)
        if registered:
            target = next(h for h in registered['hubs'] if Path(h['path']).resolve() == hub_dir.resolve())
            if model_id and model_id != target['model_id']:
                raise migrate.MigrationBlocked('model ID does not match this registered hub')
            model_id = target['model_id']
        source = create_engine(from_owui)
        try:
            migrate.plan_from_owui(source, hub_name,
                model_id=model_id, plan=plan,
                permissions=[p['permission'] for p in permission_catalog(hub_dir)],
                standalone_public=standalone_public)
        finally:
            source.dispose()
    return plan


@access_app.command("migrate")
def access_migrate(
    from_owui: str = typer.Option(None, "--from-owui", help="Open WebUI DB URL to also read (group + model access)."),
    model_id: str = typer.Option(None, "--model-id", help="Which OWUI model is this hub (required when multiple models exist)."),
    standalone_public: bool = typer.Option(False, "--standalone-public", help="Explicitly confirm legacy standalone signed-in public entry; verifies tools against the legacy CSV resolver."),
    apply: bool = typer.Option(False, "--apply", help="Actually apply + make Casbin authoritative (the cutover). Without this, dry-run."),
    remigrate: bool = typer.Option(False, "--remigrate", help="Allow re-running --apply on an already-migrated hub (OVERWRITES dashboard edits made since migration). Off by default."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Flatten legacy access (access.csv [+ Open WebUI]) into direct Casbin
    grants. Dry-run by default; --apply performs the cutover."""
    from .access import store_for
    from .access.migrate import MigrationBlocked, apply as apply_plan, diff

    try:
        plan = _build_plan(hub_dir, from_owui, model_id, standalone_public)
    except MigrationBlocked as e:
        console.print(f"[red]migration blocked:[/red] {e}")
        raise typer.Exit(2)

    console.print(f"[bold]{len(plan.grants)} grant(s), {len(plan.attrs)} attribute(s)[/bold]")
    for c in plan.conflicts:
        console.print(f"[yellow]conflict:[/yellow] {c}")
    for w in plan.warnings:
        console.print(f"[dim]{w}[/dim]")

    from .access.migrate import verify_effective
    mismatches = verify_effective(plan)
    if plan.expected:
        console.print(f"Effective access: {len(plan.expected)} legacy decisions checked, {len(mismatches)} differences")
        for row in mismatches:
            console.print(f"{row['subject']} / {row['hub']} / {row['permission']}: {row['before']} -> {row['after']}")
    else:
        console.print("[yellow]CSV-only import: OWUI model visibility has not been verified. Use --from-owui for customer cutover.[/yellow]")
    if mismatches:
        raise typer.Exit(2)
    gs = store_for(hub_dir)
    if not apply:
        d = diff(gs, plan)
        console.print(f"[dim]dry-run — vs current store: {len(d['missing'])} missing, "
                      f"{len(d['extra'])} extra. Re-run with --apply to cut over.[/dim]")
        return
    # Prevent accidental re-import over permissions edited after migration: refuse a
    # second cutover of an already dashboard-managed hub unless explicitly forced.
    if gs.is_authoritative(hub_dir.resolve().name) and not remigrate:
        console.print("[red]already migrated:[/red] this hub is dashboard-managed. "
                      "Re-importing legacy access would overwrite edits made since "
                      "migration. Pass --remigrate only if you intend to discard them.")
        raise typer.Exit(2)
    if not plan.expected:
        console.print("[red]Cutover requires --from-owui model evidence or --standalone-public for a legacy standalone hub. CSV alone cannot prove existing model access.[/red]")
        raise typer.Exit(2)
    if not plan.grants:
        console.print("[red]refusing to cut over with an empty plan (no grants). "
                      "This would lock everyone out.[/red]")
        raise typer.Exit(2)
    import time
    from .deployment import _write
    backup = hub_dir / '.hubzoid' / 'backups' / f'access-{time.time_ns()}.json'
    snapshot = gs.snapshot([hub_dir.resolve().name])
    if plan.visibility_backup is not None:
        snapshot['owui_visibility'] = plan.visibility_backup
    _write(backup, snapshot)
    console.print(f"Backup: {backup} (restore with hubzoid access rollback)")
    try:
        apply_plan(gs, plan, authoritative=True)
    except MigrationBlocked as e:
        console.print(f"[red]cutover refused:[/red] {e}")
        raise typer.Exit(2)
    d = diff(gs, plan)
    # The cutover gate is a FULL zero diff: nothing planned-but-missing AND
    # nothing stale (extra) in the store.
    ok = not d["missing"] and not d["extra"]
    colour = "green" if ok else "red"
    console.print(f"[{colour}]applied[/{colour}] · Casbin is now authoritative · "
                  f"{len(d['missing'])} missing, {len(d['extra'])} extra after apply")
    if not ok:
        console.print("[red]non-zero diff after cutover — investigate; the zero-diff "
                      "gate was not met.[/red]")
        raise typer.Exit(1)


@access_app.command("rollback")
def access_rollback(backup: Path, hub_dir: Path = typer.Argument(Path("."))) -> None:
    """Restore a pre-cutover access snapshot; does not change OWUI accounts."""
    from .access import store_for
    import getpass
    snapshot = json.loads(backup.read_text())
    if snapshot.get('hubs') != [hub_dir.resolve().name.lower()]:
        console.print("[red]Backup does not match the selected hub; no access changed.[/red]")
        raise typer.Exit(2)
    if snapshot.get('owui_visibility'):
        from .access.reconcile import validate_visibility_backup
        validate_visibility_backup(hub_dir, snapshot['owui_visibility'])
    store_for(hub_dir).restore(snapshot, actor=f"cli:{getpass.getuser()}")
    if all(snapshot['authority'].values()):
        console.print("Access snapshot restored. Run access sync and verify end-user visibility.")
    elif snapshot.get('owui_visibility'):
        from .access.reconcile import restore_visibility
        try:
            restore_visibility(hub_dir, snapshot['owui_visibility'])
        except Exception:
            console.print("[red]Hubzoid access restored, but OWUI visibility restoration failed. Keep the maintenance window open, check service credentials, then rerun rollback with the same backup.[/red]")
            raise typer.Exit(1)
        console.print("Legacy access and pre-cutover OWUI visibility restored. Verify representative end users before ending maintenance.")
    else:
        console.print("Legacy access restored. Restore the pre-cutover OWUI model ACL from your deployment backup before ending the maintenance window; access sync does not restore legacy ACLs.")


@access_app.command("diff")
def access_diff(
    from_owui: str = typer.Option(None, "--from-owui", help="Open WebUI DB URL to also read."),
    model_id: str = typer.Option(None, "--model-id", help="Which OWUI model is this hub."),
    standalone_public: bool = typer.Option(False, "--standalone-public", help="Compare the legacy standalone public-entry plan."),
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Show the full static diff between the migration plan and the live store
    (the zero-diff cutover gate)."""
    from .access import store_for
    from .access.migrate import MigrationBlocked, diff

    try:
        plan = _build_plan(hub_dir, from_owui, model_id, standalone_public)
    except MigrationBlocked as e:
        console.print(f"[red]migration blocked:[/red] {e}")
        raise typer.Exit(2)
    d = diff(store_for(hub_dir), plan)
    for kind in ("missing", "extra"):
        for subj, hub, perm in d[kind]:
            mark = "[red]-[/red]" if kind == "extra" else "[green]+[/green]"
            console.print(f"{mark} {subj:28.28} {perm:18.18} [dim]{hub}[/dim]")
    console.print(f"[dim]{len(d['missing'])} missing, {len(d['extra'])} extra[/dim]")


@access_app.command("sync")
def access_sync(
    hub_dir: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """Recompute + re-project each subject's visible hubs (the Casbin->OWUI
    visibility mirror). The recovery path if a projection was ever missed."""
    from .access.reconcile import sync_owui
    result = sync_owui(hub_dir)
    console.print(result)
    if result['state'] == 'error':
        raise typer.Exit(1)


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
from .migrate_openwebui import migrate_app  # noqa: E402

app.add_typer(
    admin_app,
    name="admin",
    help="Accounts: create administrators and users, reset passwords.",
    rich_help_panel="Commands",
)
app.add_typer(
    migrate_app,
    name="migrate",
    help="Move an Open WebUI install to the Hubzoid web app.",
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
# The 1.0 names WEBUI_AUTH, WEBUI_URL and WEBUI_ADMIN_EMAIL/_PASSWORD still work.

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

# --- Legacy: the Open WebUI chat app (this release only) -------------------
# HUBZOID_UI=openwebui           # needs: pip install "hubzoid[openwebui]"
# In legacy mode, Open WebUI's own settings apply, for example:
# WEBUI_AUTH=true                # Open WebUI sign-in
# WEBUI_SECRET_KEY=              # required with WEBUI_AUTH: openssl rand -hex 32
# WEBUI_NAME=                    # display name; blank = the agent's name
# ENABLE_MEMORY=true             # Open WebUI's per-user memories (Beta)
# HUBZOID_KEEP_OWUI_SUFFIX=True  # keep Open WebUI branding with files in ./branding/
# Hubzoid sets about 24 Open WebUI flags to strip platform surfaces; add any of
# them here to override. See docs/branding.md.
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


# ---------------------------------------------------------------------------
# eval — hub-owned behavioural checks (see hubzoid/evals/)
# ---------------------------------------------------------------------------
eval_app = typer.Typer(
    help="Hub-owned evals: one md file per case under <hub>/evals/. Run them "
    "by hand, from CI (the exit code is the gate), or on a cron via a case's "
    "`schedule:` frontmatter.",
    no_args_is_help=True,
)


def _load_cases(hub: Path, tag: str | None, case: str | None):
    """Discover + filter, turning a bad case file into a clean CLI error."""
    from .evals import cases as cases_lib

    try:
        found = cases_lib.discover(hub)
    except cases_lib.EvalCaseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if not found:
        console.print(f"no eval cases — add markdown files under {hub / 'evals'}/")
        raise typer.Exit(0)

    selected = cases_lib.select(found, tag=tag, case=case)
    if not selected:
        console.print("[yellow]no cases matched that filter[/yellow]")
        raise typer.Exit(0)
    return selected


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
) -> None:
    """Run the hub's eval cases. Exits non-zero if any fail — that is the CI gate.

    Cases run through the hub's own runtime, so they see the same model, tools,
    MCP servers and access guard that real chat traffic does. Free checks
    (substrings, tool calls) run first; a case with a `## Criteria` section is
    then graded by a model, but only if the free checks passed.
    """
    from .evals import judge as judge_lib
    from .evals import report as report_lib
    from .evals import runner as runner_lib

    hub = hub.resolve()
    selected = _load_cases(hub, tag, case)

    judged = [c for c in selected if c.is_judged]
    judge_fn = None
    if judged and not no_judge:
        judge_fn = judge_lib.make_judge(hub, model=judge_model)

    console.print(f"[cyan]{len(selected)} case(s)[/cyan] · {hub.name}")
    if judge_fn is not None:
        console.print(f"[dim]judge: {judge_lib.describe(hub, judge_model)}[/dim]")
    elif judged:
        console.print(f"[dim]judge: off — {len(judged)} case(s) will run free checks only[/dim]")

    def _progress(result) -> None:
        if quiet and result.passed:
            return
        mark = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
        console.print(f"  {mark} {result.name}"
                      + (f"  [dim]{result.reason}[/dim]" if result.reason else ""))

    suite = runner_lib.run_suite(
        hub, selected, judge_fn=judge_fn, on_case=_progress, model=model)

    previous = report_lib.load_runs(hub, limit=1)
    path = report_lib.save(hub, suite)

    console.print()
    report_lib.render_table(console, suite)
    console.print(f"[dim]{path}[/dim]")

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


def _push_to_langfuse(hub: Path, suite) -> None:
    """Best-effort push. Never fails a run — the local JSON is the record."""
    from .evals import langfuse as lf

    try:
        pushed = lf.push(hub, suite)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]langfuse push skipped: {exc}[/yellow]")
        return
    if pushed:
        console.print(f"[dim]langfuse: {pushed}[/dim]")


@eval_app.command("list")
def eval_list(
    hub: Path = typer.Argument(Path("."), help="Hub directory. Default: current dir."),
) -> None:
    """List the hub's eval cases: what each checks, and which are scheduled."""
    from . import scheduling as sch
    from .evals import cases as cases_lib

    hub = hub.resolve()
    try:
        found = cases_lib.discover(hub)
    except cases_lib.EvalCaseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if not found:
        console.print(f"no eval cases — add markdown files under {hub / 'evals'}/")
        return

    for c in found:
        bits = []
        if c.expect_tools:
            bits.append(f"expects {', '.join(c.expect_tools)}")
        if c.forbid_tools:
            bits.append(f"forbids {', '.join(c.forbid_tools)}")
        if c.contains:
            bits.append(f"contains {len(c.contains)}")
        if c.not_contains:
            bits.append(f"not_contains {len(c.not_contains)}")
        bits.append("judged" if c.is_judged else "free only")
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
) -> None:
    """Last run, pass rate, and what is currently failing.

    This is the surface that matters for scheduled evals — nobody is watching
    a terminal at 06:00 on a Monday.
    """
    from .evals import report as report_lib

    hub = hub.resolve()
    suite = report_lib.latest(hub)
    if suite is None:
        console.print("no eval runs yet — try `hubzoid eval run`")
        return

    verdict = "[green]all passing[/green]" if suite.ok else f"[red]{suite.failed} failing[/red]"
    console.print(f"{verdict}  ·  {suite.passed}/{len(suite.cases)} passed"
                  f"  ·  {suite.finished_at or suite.started_at}")
    console.print(f"[dim]model: {suite.model or '?'}"
                  + (f" · judge: {suite.judge_model}" if suite.judge_model else "")
                  + "[/dim]")
    for c in suite.cases:
        if not c.passed:
            console.print(f"  [red]✗[/red] {c.name}  [dim]{c.reason}[/dim]")


@eval_app.command("explain")
def eval_explain(
    hub: Path = typer.Argument(..., help="Hub directory."),
    case_name: str = typer.Argument(..., metavar="CASE", help="Case name (the md filename stem)."),
) -> None:
    """Everything needed to fix one failing case, in one place.

    The prompt, the full response, the tool calls, each assertion's verdict,
    the judge's reasoning, and the path to the markdown to edit — because
    "edit the instructions" is the primary fix lever, not "edit the code".
    """
    from .evals import cases as cases_lib
    from .evals import report as report_lib

    hub = hub.resolve()
    suite = report_lib.latest(hub)
    if suite is None:
        console.print("no eval runs yet — try `hubzoid eval run`")
        raise typer.Exit(1)

    result = next((c for c in suite.cases if c.name == case_name), None)
    if result is None:
        known = ", ".join(c.name for c in suite.cases) or "(none)"
        console.print(f"[red]no case {case_name!r} in the last run[/red]. Ran: {known}")
        raise typer.Exit(2)

    case = next((c for c in cases_lib.discover(hub, strict=False) if c.name == case_name), None)

    console.print(f"[bold]{result.name}[/bold]  "
                  + ("[green]PASS[/green]" if result.passed else "[red]FAIL[/red]")
                  + f"  ·  {result.duration:.1f}s")
    if case is not None and case.source_path:
        console.print(f"[dim]case file:  {case.source_path}[/dim]")
    console.print(f"[dim]hub spec:   {hub / 'AGENTS.md'}[/dim]")

    if case is not None:
        console.print("\n[cyan]prompt[/cyan]")
        console.print(case.prompt)

    console.print("\n[cyan]response[/cyan]")
    console.print(result.response or "[dim](empty)[/dim]")

    console.print("\n[cyan]tools called[/cyan]")
    console.print(", ".join(result.tool_calls) or "[dim](none)[/dim]")

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
