"""``hubzoid chat config|theme|preferences|capabilities``.

The same service as ``/api/chat/...``, run locally against the deployment's
files and operational store. ``HUB`` is the hub folder (default ``.``); in a
gateway its deployment folder holds ``chat/config.yaml``.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from ..control import canonical
from ..control import cli_common as cc
from ..control import yamlfile
from . import config as cfg
from . import theme as theme_mod

chat_app = typer.Typer(help="Chat settings, theme and personal preferences.", no_args_is_help=True)
config_app = typer.Typer(help="The deployment's chat/config.yaml.", no_args_is_help=True)
theme_app = typer.Typer(help="The theme tokens chat and Console share.", no_args_is_help=True)
prefs_app = typer.Typer(help="One person's appearance preferences.", no_args_is_help=True)
chat_app.add_typer(config_app, name="config")
chat_app.add_typer(theme_app, name="theme")
chat_app.add_typer(prefs_app, name="preferences")

HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")


def _service(hub: Path) -> cfg.ChatConfigService:
    if not Path(hub).is_dir():
        cc.fail(400, "invalid_request", f"{hub} is not a hub folder.", False)
    return cfg.ChatConfigService(Path(hub))


def _file(hub: Path, file: Path | None) -> Path:
    if file is None:
        return cfg.ChatConfigService(Path(hub)).file_path() if Path(hub).is_dir() else Path(cfg.FILE)
    return file if file.is_absolute() else (Path.cwd() / file)


def _guard(as_json: bool):
    class _G:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if isinstance(exc, cfg.ConfigError):
                cc.fail(exc.status, exc.code, exc.message, as_json, **exc.extra)
            if isinstance(exc, theme_mod.ThemeError):
                cc.fail(422, exc.code, exc.message, as_json, pointer=exc.pointer, **exc.extra)
            return False

    return _G()


@config_app.command("schema")
def config_schema(hub: Path = HUB, as_json: bool = JSON) -> None:
    """The JSON Schema of chat/config.yaml, with defaults and personal keys."""
    cc.emit(cfg.schema(), True)


@config_app.command("get")
def config_get(hub: Path = HUB, effective: bool = typer.Option(False, "--effective"),
               sources: bool = typer.Option(False, "--sources"), as_json: bool = JSON) -> None:
    """The active revision. --sources adds where each value comes from."""
    svc = _service(hub)
    with _guard(as_json):
        active = svc.active()
        out = {"revision": active.revision, "digest": active.digest, "config": active.document,
               "applied_by": active.applied_by, "applied_at": active.applied_at,
               "file": svc.file_state()}
        if effective or sources:
            out["sources"] = svc.sources()
            out["effective"] = svc.effective()

    def human(o):
        cc.console.print(f"chat/config revision [bold]{o['revision']}[/bold] {o['digest']}")
        f = o["file"]
        if not f.get("exists"):
            cc.console.print("[dim]no chat/config.yaml: product defaults apply[/dim]")
        elif not f.get("valid"):
            cc.console.print(f"[red]chat/config.yaml is not valid:[/red] {f['error']['message']}")
        elif not f.get("matches_active"):
            cc.console.print(f"[yellow]chat/config.yaml has {f['pending_changes']} change(s) not applied[/yellow]")
        for pointer, item in (o.get("sources") or {}).items():
            cc.console.print(f"  {pointer:48} {json.dumps(item['value']):24} [dim]{item['source']}[/dim]")

    cc.emit(out, as_json, human)


@config_app.command("validate")
def config_validate(hub: Path = HUB, file: Path = typer.Option(None, "--file"),
                    as_json: bool = JSON) -> None:
    """Validate a chat/config.yaml. Changes nothing."""
    path = _file(hub, file)
    with _guard(as_json):
        doc, _ = cfg.parse_file(path)
    out = {"valid": True, "normalized_config": doc, "digest": canonical.digest(doc), "warnings": []}
    cc.emit(out, as_json, lambda o: cc.console.print(f"[green]valid[/green] {o['digest']}"))


@theme_app.command("validate")
def theme_validate(hub: Path = HUB, file: Path = typer.Option(Path("branding/theme.json"), "--file"),
                   as_json: bool = JSON) -> None:
    """Validate a theme manifest and every mode's contrast. Activates nothing."""
    path = file if file.is_absolute() else Path(hub) / file
    try:
        manifest = json.loads(path.read_text())
    except FileNotFoundError:
        cc.fail(400, "invalid_request", f"{path} does not exist.", as_json)
    except ValueError as exc:
        cc.fail(422, "validation_failed", f"{path} is not valid JSON: {exc}.", as_json)
    with _guard(as_json):
        resolved = theme_mod.resolve(manifest)
    cc.emit({"valid": True, "digest": canonical.digest(resolved), "diagnostics": []}, as_json,
            lambda o: cc.console.print(f"[green]valid[/green] {o['digest']} (every mode passes contrast)"))


@theme_app.command("inspect")
def theme_inspect(hub: Path = HUB, mode: str = typer.Option("light", "--mode"),
                  as_json: bool = JSON) -> None:
    """The active resolved theme for one mode."""
    from .config_routes import active_theme

    if mode not in theme_mod.MODES:
        cc.fail(400, "invalid_request", f"--mode is one of {', '.join(theme_mod.MODES)}.", as_json)
    revision, resolved = active_theme(Path(hub))
    out = {"revision": revision, "mode": mode, "tokens": resolved["palettes"][mode],
           "typography": resolved["typography"], "shape": resolved["shape"]}
    cc.emit(out, as_json)


def _user_id(hub: Path, email: str | None, as_json: bool) -> str:
    from .. import auth
    from ..auth import users

    if not email:
        return auth.local_owner(Path(hub)).id
    record = users.find_by_email(Path(hub), email)
    if record is None:
        cc.fail(404, "not_found", f"No account for {email}.", as_json)
    return record["id"]


@prefs_app.command("get")
def prefs_get(hub: Path = HUB, user: str = typer.Option(None, "--user"), as_json: bool = JSON) -> None:
    """A person's appearance preferences and the effective values."""
    svc = _service(hub)
    cc.emit(svc.get_preferences(_user_id(hub, user, as_json)), as_json)


@prefs_app.command("set")
def prefs_set(hub: Path = HUB, user: str = typer.Option(None, "--user"),
              mode: str = typer.Option(None, "--mode"), density: str = typer.Option(None, "--density"),
              text_scale: float = typer.Option(None, "--text-scale"),
              timezone: str = typer.Option(None, "--timezone"),
              expected_revision: int = typer.Option(None, "--expected-revision"),
              as_json: bool = JSON) -> None:
    """Change a person's preferences (only the keys the deployment allows)."""
    svc = _service(hub)
    uid = _user_id(hub, user, as_json)
    with _guard(as_json):
        current = svc.get_preferences(uid)
        values = dict(current["values"])
        for key, value in (("mode", mode), ("density", density), ("text_scale", text_scale),
                           ("timezone", timezone)):
            if value is not None:
                values[key] = value
        rev = current["revision"] if expected_revision is None else expected_revision
        result = svc.set_preferences(uid, values, expected_revision=rev)
    cc.emit(result, as_json)


@chat_app.command("capabilities")
def chat_capabilities(hub: Path = HUB, as_json: bool = JSON) -> None:
    """Which chat features this install provides."""
    cc.emit(_service(hub).capabilities(), as_json)
