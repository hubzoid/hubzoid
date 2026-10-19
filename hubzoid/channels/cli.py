"""``hubzoid channels``: set up, check and operate channel connections.

    hubzoid channels list      [HUB]
    hubzoid channels validate  [HUB] --file FILE
    hubzoid channels apply     [HUB] --file FILE --expected-revision N|absent
    hubzoid channels export    [HUB] ID [--format yaml|json]
    hubzoid channels doctor    [HUB] ID [--actor PROVIDER_USER_ID] [--workspace ID] [--input KEY=VALUE]
    hubzoid channels pause     [HUB] ID --expected-revision N
    hubzoid channels resume    [HUB] ID --expected-revision N
    hubzoid channels threads   [HUB] ID
    hubzoid channels inspect   [HUB] ID --thread THREAD
    hubzoid channels deliveries [HUB] ID [--status STATUS]
    hubzoid channels redrive   [HUB] ID DELIVERY
    hubzoid channels outbox    [HUB] ID [--state STATE]
    hubzoid channels reconcile [HUB] ID OPERATION [--decision check|done|retry|abandon]
    hubzoid channels subscribe|unsubscribe [HUB] ID --thread THREAD [--expected-revision N]
    hubzoid channels link      [HUB] ID --external-id ID --account EMAIL [--workspace ID]
    hubzoid channels unlink    [HUB] ID LINK
    hubzoid channels links     [HUB] ID

Every command takes ``--json`` and prints the same object the API returns.
Exit codes: 0 done, 1 failed, 2 invalid input, 3 permission denied, 4 conflict,
5 a dependency is unavailable. Nothing here posts to a provider.
"""
from __future__ import annotations

import getpass
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()

channels_app = typer.Typer(help="Channels: Slack, GitHub, Linear, email and WhatsApp threads for this hub.",
                           no_args_is_help=True)

_EXIT = {400: 2, 401: 3, 403: 3, 404: 1, 409: 4, 413: 2, 422: 2, 429: 5, 503: 5}

HUB = typer.Argument(Path("."), help="The hub folder.")
JSON = typer.Option(False, "--json", help="Print JSON.")


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001
        return "cli:operator"


def _hub(hub: Path, as_json: bool) -> Path:
    if not hub.is_dir():
        _fail("invalid_request", f"{hub} is not a hub folder.", 400, as_json)
    return hub.resolve()


def _fail(code: str, message: str, status: int, as_json: bool, **extra) -> None:
    if as_json:
        print(json.dumps({"detail": {"code": code, "message": message, **extra}}, default=str))
    else:
        console.print(f"[red]{escape(message)}[/red]")
        for f in extra.get("fields") or []:
            where = f"{f.get('field')}" + (f" (line {f['line']})" if f.get("line") else "")
            console.print(f"  [red]{escape(where)}[/red]: {escape(f.get('message', ''))}")
    raise typer.Exit(_EXIT.get(status, 1))


def _run(fn, as_json: bool, *args, **kwargs):
    from ..work_items.errors import WorkItemError
    from .errors import ChannelError

    try:
        return fn(*args, **kwargs)
    except ChannelError as exc:
        _fail(exc.code, exc.message, exc.status, as_json, **exc.extra)
    except WorkItemError as exc:
        _fail(exc.code, exc.message, exc.status, as_json)


def _out(data, as_json: bool, render=None) -> None:
    if as_json or render is None:
        print(json.dumps(data, indent=None if as_json else 2, default=str))
    else:
        render(data)


def _expected(raw: str | None, as_json: bool):
    if raw is None:
        return None
    if raw == "absent":
        return "absent"
    try:
        return int(raw)
    except ValueError:
        _fail("validation_failed", '--expected-revision is a number or "absent".', 422, as_json)


@channels_app.command("list")
def list_cmd(hub: Path = HUB, as_json: bool = JSON):
    """Connections with status, revision and backlog."""
    from . import routes

    hub = _hub(hub, as_json)
    data = _run(routes.list_all, as_json, hub)

    def show(d):
        t = Table("id", "provider", "status", "rev", "target", "threads", "backlog", "uncertain")
        for c in d["items"]:
            t.add_row(c["id"], c["provider"], c["status"], str(c["revision"]), c["target"], str(c["threads"]),
                      str(c["backlog"]), str(c["uncertain_replies"]))
        console.print(t)
        for c in d["not_applied"]:
            console.print(f"[yellow]{escape(c['file'])} is not applied yet (hubzoid channels apply).[/yellow]")
        if not d["items"] and not d["not_applied"]:
            console.print("No channels. Add channels/<id>.yaml and apply it.")

    _out(data, as_json, show)


@channels_app.command("validate")
def validate_cmd(hub: Path = HUB, file: Path = typer.Option(..., "--file", help="The channel file."),
                 as_json: bool = JSON):
    """Check a channel file without activating it. No network writes."""
    from . import config

    hub = _hub(hub, as_json)
    result = config.load_file(hub, file)
    data = result.to_json()
    if not result.valid:
        _fail("validation_failed", f"{file} is not valid.", 422, as_json, fields=result.errors)

    def show(d):
        console.print(f"[green]{escape(str(file))} is valid.[/green] Provider {d['capabilities']['provider']}, "
                      f"target {d['normalized'].get('agent') or d['normalized'].get('workflow')}, "
                      f"network_writes: false")

    _out(data, as_json, show)


@channels_app.command("apply")
def apply_cmd(hub: Path = HUB, file: Path = typer.Option(..., "--file", help="The channel file."),
              expected_revision: str = typer.Option(..., "--expected-revision",
                                                    help='The revision you read, or "absent" to create.'),
              as_json: bool = JSON):
    """Validate the file, save it as channels/<id>.yaml and make it active."""
    from . import config

    hub = _hub(hub, as_json)
    result = config.load_file(hub, file)
    if not result.valid:
        _fail("validation_failed", f"{file} is not valid.", 422, as_json, fields=result.errors)
    text = Path(file).read_text(encoding="utf-8")
    out = _run(config.apply, as_json, hub, result.data, expected_revision=_expected(expected_revision, as_json),
               actor=_actor(), text=text)
    _out(out, as_json, lambda d: console.print(
        f"[green]{d['id']} applied: revision {d['revision']}, {d['status']}.[/green] File {escape(d['file'])}. "
        "A running hub picks it up within seconds."))


@channels_app.command("export")
def export_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
               fmt: str = typer.Option("yaml", "--format", help="yaml or json"), as_json: bool = JSON):
    """The active configuration, with secret references and never values."""
    from . import config

    hub = _hub(hub, as_json)
    data = _run(config.export, as_json, hub, cid)
    if as_json or fmt == "json":
        print(json.dumps(data, default=str))
        return
    console.print(f"# {cid}: revision {data['revision']}, {data['status']}")
    print(config.render(data["config"]), end="")


@channels_app.command("doctor")
def doctor_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
               actor: str = typer.Option(None, "--actor", help="A provider user id to check end to end."),
               workspace: str = typer.Option(None, "--workspace", help="The provider workspace of --actor."),
               inputs: list[str] = typer.Option([], "--input", help="Provider check input KEY=VALUE."),
               as_json: bool = JSON):
    """Read-only checks. Never posts or subscribes."""
    from . import config, doctor

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    extra = {}
    for item in inputs:
        k, _, v = item.partition("=")
        extra[k.strip()] = v.strip()
    data = _run(doctor.run, as_json, conn, {**extra, "actor": actor, "workspace_id": workspace})

    def show(d):
        colors = {"ok": "green", "warn": "yellow", "fail": "red", "skip": "dim"}
        for c in d["checks"]:
            line = f"[{colors.get(c['status'], 'white')}]{c['status']:>4}[/] {escape(c['id'])}: {escape(c['message'])}"
            console.print(line)
            if c.get("recovery") and c["status"] in ("fail", "warn"):
                console.print(f"      [dim]{escape(c['recovery'])}[/dim]")
        console.print("[green]Ready.[/green]" if d["ready"] else "[yellow]Not ready.[/yellow]")

    _out(data, as_json, show)
    if not data["ready"] and any(c["status"] == "fail" for c in data["checks"]):
        raise typer.Exit(1)


def _toggle(hub: Path, cid: str, enabled: bool, expected_revision: int, as_json: bool):
    from . import config

    hub = _hub(hub, as_json)
    out = _run(config.set_enabled, as_json, hub, cid, enabled, expected_revision=expected_revision, actor=_actor())
    _out(out, as_json, lambda d: console.print(f"{d['id']} is {d['status']} (revision {d['revision']})."))


@channels_app.command("pause")
def pause_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
              expected_revision: int = typer.Option(..., "--expected-revision"), as_json: bool = JSON):
    """Stop new intake (enabled: false). Accepted work and its replies continue."""
    _toggle(hub, cid, False, expected_revision, as_json)


@channels_app.command("resume")
def resume_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
               expected_revision: int = typer.Option(..., "--expected-revision"), as_json: bool = JSON):
    """Start new intake again (enabled: true). Run doctor first."""
    _toggle(hub, cid, True, expected_revision, as_json)


@channels_app.command("threads")
def threads_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                limit: int = typer.Option(50, "--limit"), as_json: bool = JSON):
    """Threads this connection has seen, newest first."""
    from . import config, routes, store

    hub = _hub(hub, as_json)
    _run(config.active, as_json, hub, cid)
    out = store.list_threads(hub, config.hub_of(hub), cid, limit=limit)
    data = {"items": [routes.thread_view(t) for t in out["items"]], "next_cursor": out["next_cursor"]}

    def show(d):
        t = Table("thread", "key", "subscribed", "requester", "work")
        for r in d["items"]:
            t.add_row(r["id"], r["thread_key"], "yes" if r["subscribed"] else "no", r["requester"] or "",
                      r["work_item_id"] or ", ".join(r["conversations"].values()))
        console.print(t)

    _out(data, as_json, show)


@channels_app.command("inspect")
def inspect_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                thread: str = typer.Option(..., "--thread"), as_json: bool = JSON):
    """One thread: origin, deliveries, replies and the work it is bound to."""
    from . import config, routes

    hub = _hub(hub, as_json)
    data = _run(routes.thread_detail, as_json, hub, config.hub_of(hub), cid, thread)
    _out(data, as_json, None)


@channels_app.command("deliveries")
def deliveries_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                   status: str = typer.Option(None, "--status"), limit: int = typer.Option(50, "--limit"),
                   as_json: bool = JSON):
    """Verified deliveries and what happened to each."""
    from . import config, routes, store

    hub = _hub(hub, as_json)
    _run(config.active, as_json, hub, cid)
    out = store.list_deliveries(hub, config.hub_of(hub), cid, status=status, limit=limit)
    data = {"items": [routes.delivery_view(d) for d in out["items"]], "next_cursor": out["next_cursor"]}

    def show(d):
        t = Table("delivery", "kind", "status", "reason", "thread")
        for r in d["items"]:
            t.add_row(r["id"], r["kind"], r["status"], r["reason"] or "", r["thread_id"] or "")
        console.print(t)

    _out(data, as_json, show)


@channels_app.command("redrive")
def redrive_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                delivery: str = typer.Argument(...), as_json: bool = JSON):
    """Route a failed, denied or paused delivery again, under its original identity and today's permissions. Never accepts new bytes."""
    from . import config, routes

    hub = _hub(hub, as_json)
    out = _run(routes.redrive, as_json, hub, config.hub_of(hub), cid, delivery, _actor())
    _out(out, as_json, lambda d: console.print(f"{d['id']} is {d['status']} again. A running hub routes it."))


@channels_app.command("outbox")
def outbox_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
               state: str = typer.Option(None, "--state"), as_json: bool = JSON):
    """Replies recorded for this connection, with their outcome."""
    from . import config, replies

    hub = _hub(hub, as_json)
    _run(config.active, as_json, hub, cid)
    data = {"items": replies.outbox(hub, config.hub_of(hub), cid, state=state)}

    def show(d):
        t = Table("operation", "kind", "state", "thread", "attempts")
        for r in d["items"]:
            t.add_row(r["id"], r["kind"], r["state"], r["thread_id"] or "", str(r["attempts"]))
        console.print(t)

    _out(data, as_json, show)


@channels_app.command("reconcile")
def reconcile_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                  operation: str = typer.Argument(...),
                  decision: str = typer.Option("check", "--decision", help="check, done, retry or abandon"),
                  evidence: str = typer.Option(None, "--evidence", help="Where you confirmed it (for done)."),
                  accept_duplicate_risk: bool = typer.Option(False, "--accept-duplicate-risk"),
                  as_json: bool = JSON):
    """Settle an uncertain reply. `check` only looks; nothing is resent unless you choose retry and accept the duplicate risk."""
    from . import config, replies

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    out = _run(replies.resolve, as_json, conn, operation, decision=decision, actor=_actor(),
               evidence={"note": evidence} if evidence else None,
               acknowledge_duplicate_risk=accept_duplicate_risk)
    _out(out, as_json, None)


def _subscription(hub: Path, cid: str, thread: str, subscribed: bool, expected_revision, as_json: bool):
    from . import config, routes, store

    hub = _hub(hub, as_json)
    _run(store.require_thread, as_json, hub, config.hub_of(hub), cid, thread)
    row = _run(store.update_thread, as_json, hub, thread, expected_revision=expected_revision,
               subscribed=1 if subscribed else 0)
    _out(routes.thread_view(row), as_json, lambda d: console.print(
        f"{thread} is {'subscribed' if subscribed else 'unsubscribed'} (revision {d['revision']})."))


@channels_app.command("subscribe")
def subscribe_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                  thread: str = typer.Option(..., "--thread"),
                  expected_revision: int = typer.Option(None, "--expected-revision"), as_json: bool = JSON):
    """Follow-ups in this thread continue the work without a new mention."""
    _subscription(hub, cid, thread, True, expected_revision, as_json)


@channels_app.command("unsubscribe")
def unsubscribe_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                    thread: str = typer.Option(..., "--thread"),
                    expected_revision: int = typer.Option(None, "--expected-revision"), as_json: bool = JSON):
    """Follow-ups need a new mention. Accepted work still replies here."""
    _subscription(hub, cid, thread, False, expected_revision, as_json)


@channels_app.command("link")
def link_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
             external_id: str = typer.Option(..., "--external-id", help="The provider's user id."),
             account: str = typer.Option(..., "--account", help="The Hubzoid account email."),
             workspace: str = typer.Option(None, "--workspace", help="The provider workspace."),
             display: str = typer.Option("", "--display"), as_json: bool = JSON):
    """Let a provider account act as a Hubzoid account in this hub."""
    from . import config, links

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    out = _run(links.link, as_json, conn, external_id=external_id, account=account, workspace_id=workspace,
               display=display, actor=_actor())
    _out(out, as_json, lambda d: console.print(
        f"[green]{d['provider']} {d['external_id']} now acts as {d['account']}.[/green]"))


@channels_app.command("unlink")
def unlink_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"), link: str = typer.Argument(...),
               as_json: bool = JSON):
    """Remove an account link. Their next message gets the not-linked notice."""
    from . import config, links

    hub = _hub(hub, as_json)
    _run(config.active, as_json, hub, cid)
    _run(links.unlink, as_json, hub, link, actor=_actor())
    _out({"id": link, "revoked": True}, as_json, lambda d: console.print(f"Link {link} removed."))


@channels_app.command("links")
def links_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"), as_json: bool = JSON):
    """Account links for this connection's provider."""
    from . import config, links

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    data = {"items": links.links_for(conn)}

    def show(d):
        t = Table("link", "workspace", "external id", "account", "display")
        for r in d["items"]:
            t.add_row(r["id"], r["workspace_id"], r["external_id"], r["account"], r["display"])
        console.print(t)

    _out(data, as_json, show)


def _providers() -> None:
    """Provider command groups: `hubzoid channels slack ...` and the rest."""
    import importlib

    for name, module in (("slack", "hubzoid.channels.slack.cli"), ("github", "hubzoid.channels.github.cli"),
                         ("linear", "hubzoid.channels.linear.cli")):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        channels_app.add_typer(getattr(mod, f"{name}_app"), name=name)


_providers()
