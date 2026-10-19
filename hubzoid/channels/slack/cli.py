"""``hubzoid channels slack``: the app manifest and moving an existing Slack
setup onto a channel file.

    hubzoid channels slack manifest      [HUB] ID [--output FILE] [--format json|yaml]
    hubzoid channels slack import-legacy [HUB] [--id slack-main] [--output FILE]
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
slack_app = typer.Typer(help="Slack: app manifest and moving an existing Slack setup.", no_args_is_help=True)

HUB = typer.Argument(Path("."), help="The hub folder.")


@slack_app.command("manifest")
def manifest_cmd(hub: Path = HUB, cid: str = typer.Argument(..., metavar="ID"),
                 output: Path = typer.Option(None, "--output", help="Write the manifest to this file."),
                 fmt: str = typer.Option("json", "--format", help="json or yaml"),
                 as_json: bool = typer.Option(False, "--json")):
    """The Slack app manifest for this connection. It holds no credentials."""
    from ..cli import _fail, _hub, _run
    from .. import config
    from . import manifest

    hub = _hub(hub, as_json)
    conn = _run(config.active, as_json, hub, cid)
    if conn.provider != "slack":
        _fail("not_found", f"{cid} is not a Slack connection.", 404, as_json)
    text = manifest.render(conn, fmt)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        if as_json:
            print(json.dumps({"file": str(output), "revision": conn.revision}))
        else:
            console.print(f"[green]Wrote {escape(str(output))}[/green] for revision {conn.revision}. In Slack: "
                          "api.slack.com/apps, Create New App, From a manifest, then paste it.")
        return
    print(text)


@slack_app.command("import-legacy")
def import_legacy_cmd(hub: Path = HUB, cid: str = typer.Option("slack-main", "--id"),
                      output: Path = typer.Option(None, "--output", help="Default channels/<id>.yaml"),
                      force: bool = typer.Option(False, "--force", help="Replace an existing channel file"),
                      as_json: bool = typer.Option(False, "--json")):
    """Write a disabled channel file for today's SLACK_BOT_TOKEN and SLACK_APP_TOKEN setup. The tokens stay where they are (env: references); no value is copied. The workspace and app ids are read from Slack with the bot token (read-only)."""
    from ...config_secrets import apply_layers
    from ..cli import _fail, _hub
    from .. import config

    hub = _hub(hub, as_json)
    try:
        apply_layers(hub, secrets=False)    # the hub's .env files, as the bridge reads them
    except Exception:  # noqa: BLE001 - the process environment still counts
        pass
    env = dict(os.environ)
    if not (env.get("SLACK_BOT_TOKEN") or "").strip():
        _fail("credential_missing", "SLACK_BOT_TOKEN is not set for this hub, so there is no Slack setup to import.",
              422, as_json)
    team, app = "TREPLACEME", "AREPLACEME"
    try:
        from slack_sdk import WebClient

        web = WebClient(token=env["SLACK_BOT_TOKEN"].strip())
        auth = web.auth_test()
        team = auth.get("team_id") or team
        app = ((web.bots_info(bot=auth.get("bot_id")).get("bot") or {}).get("app_id")) or app
    except Exception:  # noqa: BLE001 - offline: the placeholders fail validation with a clear message
        pass
    data = {"version": 1, "id": cid, "provider": "slack", "enabled": False, "agent": "main",
            "allowed_groups": [], "provider_config": {
                "transport": "socket_mode", "installation_team_id": team, "app_id": app,
                "bot_token_ref": "env:SLACK_BOT_TOKEN", "app_token_ref": "env:SLACK_APP_TOKEN",
                "allowed_channel_ids": [], "allow_direct_messages": True}}
    target = output or (config.channels_dir(hub, create=True) / f"{cid}.yaml")
    if target.exists() and not force:
        _fail("already_exists", f"{target} already exists. Nothing was changed. Pass --force to replace it.", 409,
              as_json)
    text = ("# Imported from the Slack environment variables. Before enabling:\n"
            "# 1. list the groups who may ask in allowed_groups and the channel ids in allowed_channel_ids\n"
            "# 2. link each person: hubzoid channels link <hub> " + cid + " --external-id U... --account email\n"
            "# 3. apply it, run doctor, stop the old `hubzoid slack run` listener, then set enabled: true\n"
            + config.render(data))
    target.write_text(text, encoding="utf-8")
    out = {"file": str(target), "installation_team_id": team, "app_id": app, "enabled": False}
    if as_json:
        print(json.dumps(out))
    else:
        console.print(f"[green]Wrote {escape(str(target))}[/green] (disabled). Workspace {team}, app {app}. "
                      "Fill allowed_groups and allowed_channel_ids, then hubzoid channels apply.")
