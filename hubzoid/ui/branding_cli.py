"""``hubzoid branding ...``: the same publication service as the API.

A builder's assistant runs: inspect → (import-legacy) → write branding.yaml and
files → validate → draft create → diff → review → activate → export. Every
step prints the API's JSON with ``--json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from ..control import cli_common as cc
from ..control import yamlfile
from . import branding_models as models
from . import branding_store as store
from .branding_models import BrandError

branding_app = typer.Typer(help="Deployment branding: draft, preview, review, activate, roll back.",
                           no_args_is_help=True)
draft_app = typer.Typer(help="Branding drafts.", no_args_is_help=True)
branding_app.add_typer(draft_app, name="draft")

HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")


def _svc(hub: Path) -> store.BrandingService:
    if not Path(hub).is_dir():
        cc.fail(400, "invalid_request", f"{hub} is not a hub folder.", False)
    return store.BrandingService(Path(hub))


class _guard:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, BrandError):
            cc.fail(exc.status, exc.code, exc.message, self.as_json, **exc.extra)
        return False


@branding_app.command("inspect")
def inspect(hub: Path = HUB, as_json: bool = JSON) -> None:
    """Where the brand is authored, what is active, and legacy files."""
    svc = _svc(hub)
    with _guard(as_json):
        _conf, _files, report = svc.legacy_import()
        out = {"deployment_id": svc.deployment_id, "folder": str(svc.root),
               "file": models.FILE, "file_exists": (svc.root / models.FILE).is_file(),
               "theme_file_exists": (svc.root / models.THEME_FILE).is_file(),
               "active": svc.active_state(), "drafts": len(svc.list_drafts()), "legacy": report}
    cc.emit(out, as_json)


@branding_app.command("import-legacy")
def import_legacy(hub: Path = HUB, write: bool = typer.Option(False, "--write",
                  help="Write branding.yaml (refuses to overwrite one)."), as_json: bool = JSON) -> None:
    """Map a pre-2.0 branding/ folder to branding.yaml and report each file."""
    svc = _svc(hub)
    with _guard(as_json):
        configuration, _files, report = svc.legacy_import()
    target = svc.root / models.FILE
    if write:
        if target.exists():
            cc.fail(409, "conflict", f"{models.FILE} already exists. Edit it instead.", as_json)
        target.write_text(yamlfile.dumps(configuration))
    cc.emit({"configuration": configuration, "report": report, "written": write and str(target)}, as_json)


@branding_app.command("validate")
def validate(hub: Path = HUB, check: bool = typer.Option(True, "--check/--no-check"),
             as_json: bool = JSON) -> None:
    """Validate branding.yaml, branding/theme.json and every referenced file."""
    from ..chat import theme as theme_mod
    from . import branding_assets, branding_css

    svc = _svc(hub)
    with _guard(as_json):
        configuration, theme, files = svc.read_source()
        refs = models.referenced_paths(configuration)
        missing = [p for p in refs.values() if p not in files]
        for key, path in refs.items():
            if path in files:
                if key == "decorative_css":
                    branding_css.parse(files[path].decode("utf-8", "replace"), path)
                else:
                    branding_assets.check(key, files[path], path)
        try:
            theme_mod.resolve(theme) if theme else theme_mod.resolve()
        except theme_mod.ThemeError as exc:
            raise BrandError(422, exc.code, exc.message, pointer=exc.pointer, **exc.extra)
        if missing:
            raise BrandError(422, "missing_asset", "These files are referenced but missing: "
                             + ", ".join(missing), paths=missing)
    cc.emit({"valid": True, "files": sorted(refs.values())}, as_json,
            lambda o: cc.console.print(f"[green]valid[/green] ({len(o['files'])} file(s))"))


@draft_app.command("create")
def draft_create(hub: Path = HUB, idempotency_key: str = typer.Option(None, "--idempotency-key"),
                 as_json: bool = JSON) -> None:
    """Upload branding.yaml, theme.json and files as a new draft."""
    svc = _svc(hub)
    with _guard(as_json):
        configuration, theme, files = svc.read_source()
        draft = svc.create_draft(configuration, theme, files, actor=cc.actor(),
                                 idempotency_key=idempotency_key, source="cli")
    out = draft.public()

    def human(o):
        cc.console.print(f"draft [bold]{o['id']}[/bold] revision {o['revision']} state {o['state']}")
        cc.console.print(f"digest {o['digest']}")
        for p in o["validation"]["problems"]:
            cc.console.print(f"  [yellow]{p['code']}[/yellow] {p['message']}")
        cc.console.print(f"preview: {o['preview_url']}")

    cc.emit(out, as_json, human)


@draft_app.command("list")
def draft_list(hub: Path = HUB, as_json: bool = JSON) -> None:
    cc.emit({"items": _svc(hub).list_drafts()}, as_json)


@branding_app.command("diff")
def diff(draft_id: str, hub: Path = HUB, as_json: bool = JSON) -> None:
    """What activating this draft would change."""
    with _guard(as_json):
        cc.emit(_svc(hub).diff(draft_id), as_json)


@branding_app.command("preview")
def preview(draft_id: str, hub: Path = HUB, as_json: bool = JSON) -> None:
    """The resolved candidate, and the private preview page to open."""
    with _guard(as_json):
        out = _svc(hub).preview(draft_id)
    cc.emit({"preview_url": f"/branding-preview/{draft_id}", **out}, as_json)


@branding_app.command("review")
def review(draft_id: str, hub: Path = HUB, digest: str = typer.Option(..., "--digest"),
           expected_revision: int = typer.Option(..., "--expected-revision",
                                                 help="The draft revision you reviewed."),
           decision: str = typer.Option(..., "--decision", help="approve or reject"),
           reason: str = typer.Option(None, "--reason"),
           idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Approve or reject the exact digest of a draft."""
    with _guard(as_json):
        out = _svc(hub).review(draft_id, digest=digest, expected_revision=expected_revision,
                               decision=decision, actor=cc.actor(), reason=reason,
                               idempotency_key=idempotency_key)
    cc.emit(out, as_json)


@branding_app.command("activate")
def activate(draft_id: str, hub: Path = HUB, review_id: str = typer.Option(..., "--review"),
             expected_revision: int = typer.Option(..., "--expected-revision",
                                                   help="The active generation you saw (0 when none)."),
             idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Activate a reviewed draft."""
    with _guard(as_json):
        out = _svc(hub).activate(draft_id, review_id=review_id, expected_generation=expected_revision,
                                 actor=cc.actor(), idempotency_key=idempotency_key)
    cc.emit(out, as_json)


@branding_app.command("history")
def history(hub: Path = HUB, as_json: bool = JSON) -> None:
    svc = _svc(hub)
    cc.emit({"items": svc.history(), "active": svc.active_state()}, as_json)


@branding_app.command("rollback")
def rollback(hub: Path = HUB, revision: str = typer.Option(..., "--revision"),
             idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """A new draft from a retained revision (review and activate it next)."""
    with _guard(as_json):
        draft = _svc(hub).rollback(revision, actor=cc.actor(), idempotency_key=idempotency_key)
    cc.emit(draft.public(), as_json)


@branding_app.command("export")
def export(hub: Path = HUB, revision: str = typer.Option(None, "--revision"),
           output: Path = typer.Option(Path("brand-export"), "--output"), as_json: bool = JSON) -> None:
    """Write an activated revision's files to a folder."""
    import io
    import zipfile

    svc = _svc(hub)
    with _guard(as_json):
        rev = revision or svc.active_state().get("revision")
        if not rev:
            raise BrandError(404, "not_found", "No brand has been activated yet.")
        data = svc.export(rev)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            target = (output / name).resolve()
            if output.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
    cc.emit({"revision": rev, "output": str(output)}, as_json)


@branding_app.command("set")
def set_key(key: str = typer.Argument(..., help="Dotted key, e.g. identity.name"), hub: Path = HUB,
            value_json: str = typer.Option(..., "--value-json"), as_json: bool = JSON) -> None:
    """Edit branding.yaml locally (validated). Publishes nothing."""
    svc = _svc(hub)
    path = svc.root / models.FILE
    data = {"version": 1}
    if path.exists():
        try:
            data = yamlfile.load(path).data
        except yamlfile.YamlError as exc:
            cc.fail(422, "invalid_yaml", exc.message, as_json, file=exc.path, line=exc.line)
    try:
        value = json.loads(value_json)
    except ValueError:
        cc.fail(400, "invalid_request", "--value-json is not valid JSON.", as_json)
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    with _guard(as_json):
        models.validate(data)
    path.write_text(yamlfile.dumps(data))
    cc.emit({"file": str(path), "key": key, "value": value}, as_json)
