"""``hubzoid branding ...``: look at and check the brand, which lives in the
deployment folder (``brand:`` in hubzoid.yaml, files in ``branding/``).

A builder's assistant runs: inspect → (import-legacy) → edit the brand and its
files → validate. Every agent picks the change up by itself; history is Git.
Every step prints JSON with ``--json``.
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from ..control import yamlfile
from . import branding_models as models
from . import branding_store as store
from .branding_models import BrandError

branding_app = typer.Typer(help="The deployment's brand: inspect and validate the folder.",
                           no_args_is_help=True)

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
               "active": svc.active_state(), "legacy": report}
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
def validate(hub: Path = HUB, as_json: bool = JSON) -> None:
    """Check the brand as every agent will read it: the brand section (or
    branding.yaml), branding/theme.json and every file it names."""
    svc = _svc(hub)
    with _guard(as_json):
        src = svc.active_source()
        if src is None:
            cc.emit({"valid": True, "files": [], "source": "default"}, as_json,
                    lambda o: cc.console.print("No brand file: the product brand is shown."))
            return
        refs = models.referenced_paths(src["configuration"])
        have = {a["path"] for a in src["assets"]}
        missing = sorted(p for p in refs.values() if p not in have)
        if missing:
            raise BrandError(422, "missing_asset", "These files are referenced but missing or unusable: "
                             + ", ".join(missing), paths=missing)
    cc.emit({"valid": True, "files": sorted(refs.values()), "source": src["source"]}, as_json,
            lambda o: cc.console.print(f"[green]valid[/green] {o['source']} ({len(o['files'])} file(s))"))
