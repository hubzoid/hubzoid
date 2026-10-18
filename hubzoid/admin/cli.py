"""``hubzoid admin config|domain|diagnostics|capabilities|overview``.

Added to the existing ``hubzoid admin`` group (accounts). Local mode acts on
the deployment's files and store as the operator at the keyboard.
"""
from __future__ import annotations

from pathlib import Path

import typer

from ..control import cli_common as cc
from . import config as admin_cfg
from . import diagnostics as diag
from .config import AdminError

config_app = typer.Typer(help="admin.yaml: diagnostic limits, through a reviewed plan.", no_args_is_help=True)
domain_app = typer.Typer(help="The settings registry.", no_args_is_help=True)
diagnostics_app = typer.Typer(help="Read-only diagnostic reports and support bundles.", no_args_is_help=True)

HUB = typer.Argument(Path("."), help="Hub folder. Default: current folder.")
JSON = typer.Option(False, "--json", help="Print the API's JSON object.")


class _guard:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if isinstance(exc, AdminError):
            cc.fail(exc.status, exc.code, exc.message, self.as_json, **exc.extra)
        return False


class _LocalOwner:
    """The operator at the keyboard: owns the local deployment."""

    org_admin = True
    hubs: tuple = ()
    email = cc.actor()

    def can(self, capability: str, hub: str | None = None) -> bool:
        return True

    def capabilities(self, hub: str | None = None) -> list[str]:
        from ..control.authz import CAPABILITIES

        return sorted(CAPABILITIES)


@config_app.command("show")
def config_show(hub: Path = HUB, as_json: bool = JSON) -> None:
    """Values, defaults, where each comes from, and the revision."""
    with _guard(as_json):
        out = admin_cfg.service_for(hub).show()
    out.pop("schema", None)
    cc.emit(out, as_json)


@config_app.command("validate")
def config_validate(hub: Path = HUB, file: Path = typer.Option(Path("admin.yaml"), "--file"),
                    check: bool = typer.Option(True, "--check/--no-check"), as_json: bool = JSON) -> None:
    """Validate an admin.yaml. Starts nothing and contacts nothing."""
    svc = admin_cfg.service_for(hub)
    path = file if file.is_absolute() else svc.root / file
    with _guard(as_json):
        values = svc.parse_file(path)
    defaulted = sorted(k for k, v in values["diagnostics"].items()
                       if v == admin_cfg.defaults()["diagnostics"][k])
    cc.emit({"valid": True, "values": values, "defaulted_keys": defaulted}, as_json)


@config_app.command("plan")
def config_plan(hub: Path = HUB, file: Path = typer.Option(Path("admin.yaml"), "--file"),
                idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Record the file's values as a plan: digest, diff and expiry."""
    svc = admin_cfg.service_for(hub)
    path = file if file.is_absolute() else svc.root / file
    with _guard(as_json):
        values = svc.parse_file(path)
        out = svc.plan(values, expected_revision=svc.active().revision, actor=cc.actor(),
                       idempotency_key=idempotency_key)

    def human(o):
        cc.console.print(f"plan [bold]{o['plan_id']}[/bold] digest {o['digest']} (base revision {o['base_revision']})")
        for c in o["diff"]:
            cc.console.print(f"  {c['pointer']}: {c['old']} -> {c['new']}")
        if not o["diff"]:
            cc.console.print("  [dim]no changes[/dim]")

    cc.emit(out, as_json, human)


@config_app.command("apply")
def config_apply(hub: Path = HUB, plan: str = typer.Option(..., "--plan"), digest: str = typer.Option(..., "--digest"),
                 expected_revision: int = typer.Option(..., "--expected-revision"),
                 idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Apply a plan by id and digest."""
    with _guard(as_json):
        out = admin_cfg.service_for(hub).apply(plan, digest=digest, expected_revision=expected_revision,
                                               actor=cc.actor(), idempotency_key=idempotency_key)
    cc.emit(out, as_json)


@config_app.command("export")
def config_export(hub: Path = HUB, domain: str = typer.Option("admin", "--domain"),
                  fmt: str = typer.Option("yaml", "--format"), out: Path = typer.Option(None, "--out"),
                  as_json: bool = JSON) -> None:
    """Write the active document (yaml) or print secret-free metadata (bundle)."""
    if domain != "admin":
        cc.fail(422, "validation_failed", "Export other domains with their owner: hubzoid chat config export, "
                "hubzoid branding export.", as_json)
    svc = admin_cfg.service_for(hub)
    if fmt == "bundle":
        shown = svc.show()
        cc.emit({"format": "bundle", "domain": "admin", "version": 1, "source_revision": shown["revision"],
                 "values": shown["values"], "omitted_secret_fields": [], "restart_required": False}, as_json)
        return
    target = out or svc.root / admin_cfg.FILE
    target.write_text(svc.export_yaml())
    cc.emit({"format": "yaml", "domain": "admin", "file": str(target), "revision": svc.active().revision}, as_json)


@domain_app.command("show")
def domain_show(domain: str = typer.Argument(None), hub: Path = HUB, as_json: bool = JSON) -> None:
    """One settings domain, or all of them."""
    items = admin_cfg.domains(hub, _LocalOwner())
    if domain:
        items = [d for d in items if d["domain"] == domain]
        if not items:
            cc.fail(404, "not_found", f"No settings domain {domain!r}.", as_json)
        cc.emit(items[0], as_json)
        return
    cc.emit({"items": items}, as_json)


@diagnostics_app.command("checks")
def diag_checks(hub: Path = HUB, as_json: bool = JSON) -> None:
    cc.emit({"items": diag.catalog()}, as_json)


def _print_report(o):
    cc.console.print(f"report [bold]{o['report_id']}[/bold] {o['status']}")
    colours = {"ok": "green", "info": "cyan", "warn": "yellow", "fail": "red", "skipped": "dim", "timeout": "magenta"}
    for c in o["checks"]:
        colour = colours.get(c["status"], "white")
        cc.console.print(f"  [{colour}]{c['status']:8}[/{colour}] {c['check_id']:26} {c['summary']}")
        if c.get("remediation") and c["status"] in ("warn", "fail", "timeout"):
            cc.console.print(f"           [dim]→ {c['remediation']}[/dim]")


@diagnostics_app.command("run")
def diag_run(hub: Path = HUB, profile: str = typer.Option("passive", "--profile"),
             check: list[str] = typer.Option(None, "--check"),
             consent_network: bool = typer.Option(False, "--consent-network"),
             wait: bool = typer.Option(False, "--wait"),
             idempotency_key: str = typer.Option(None, "--idempotency-key"), as_json: bool = JSON) -> None:
    """Start a report; --wait prints it when done."""
    svc = diag.service_for(hub)
    with _guard(as_json):
        started = svc.start(actor=cc.actor(), profile=profile, check_ids=list(check) if check else None,
                            consent_network=consent_network, idempotency_key=idempotency_key, run_inline=wait)
        if not wait:
            cc.emit(started, as_json)
            raise typer.Exit(cc.EXIT_PENDING)
        report = svc.get(started["report_id"])
    cc.emit(report, as_json, _print_report)
    if report["status"] != "completed" or report["counts"]["fail"]:
        raise typer.Exit(cc.EXIT_FAILED)


@diagnostics_app.command("list")
def diag_list(hub: Path = HUB, as_json: bool = JSON) -> None:
    cc.emit(diag.service_for(hub).list(), as_json)


@diagnostics_app.command("show")
def diag_show(report_id: str, hub: Path = HUB, as_json: bool = JSON) -> None:
    with _guard(as_json):
        cc.emit(diag.service_for(hub).get(report_id), as_json, _print_report)


@diagnostics_app.command("cancel")
def diag_cancel(report_id: str, hub: Path = HUB, as_json: bool = JSON) -> None:
    with _guard(as_json):
        cc.emit(diag.service_for(hub).cancel(report_id, actor=cc.actor()), as_json)


@diagnostics_app.command("bundle")
def diag_bundle(report_id: str, hub: Path = HUB, out: Path = typer.Option(None, "--out"),
                as_json: bool = JSON) -> None:
    """A metadata-only support bundle; --out saves and verifies it."""
    import hashlib

    svc = diag.service_for(hub)
    with _guard(as_json):
        info = svc.bundle(report_id, actor=cc.actor())
        if out:
            data = svc.bundle_bytes(info["bundle_id"], actor=cc.actor())
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                cc.fail(503, "unavailable", "The bundle did not verify.", as_json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            info["saved_to"] = str(out)
    cc.emit(info, as_json)


def capabilities(hub: Path = HUB, as_json: bool = JSON) -> None:
    """What this operator may do, the settings domains and diagnostic checks."""
    owner = _LocalOwner()
    cc.emit({"principal": {"email": owner.email, "org_admin": True}, "capabilities": owner.capabilities(),
             "domains": admin_cfg.domains(hub, owner), "diagnostics": diag.catalog()}, as_json)


def overview(hub: Path = HUB, days: int = typer.Option(7, "--days"), as_json: bool = JSON) -> None:
    """Deployment cards (unknown is null, never zero)."""
    from .routes import overview as build

    cc.emit(build(hub, _LocalOwner(), days), as_json)


def register(admin_app: typer.Typer) -> None:
    admin_app.add_typer(config_app, name="config")
    admin_app.add_typer(domain_app, name="domain")
    admin_app.add_typer(diagnostics_app, name="diagnostics")
    admin_app.command("capabilities")(capabilities)
    admin_app.command("overview")(overview)

