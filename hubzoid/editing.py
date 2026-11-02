"""Changing an agent's files from the browser (Beta, off by default).

An agent's instructions, skills, connectors, channels, approval rules, usage
limits and settings are files. On a laptop the builder edits them, or asks
Claude Code or Codex. On a server they come from Git and arrive with a deploy:
a change written on the server is not in the repository, and the next pull or
redeploy conflicts with it or drops it. So the browser reads these files, and
changes them only when a deployment turns this on (``in_app_editing: true`` in
``hubzoid.yaml``, or ``HUBZOID_IN_APP_EDITING=true`` for one hub), and never
where the files come from an image.

Only the browser routes and the agent's own "propose a shared change" tools
check this. The CLI's ``apply`` commands run where the builder works and keep
working. Workflow permissions and secrets live in the database, not in files,
and stay editable. Connectors are files too, but adding one is how an agent
reaches an app, so an administrator adds and edits them from the browser with
the switch off (``always=True``); only files from an image refuse.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV = "HUBZOID_IN_APP_EDITING"
CODE = "in_app_editing_off"


def enabled(hub_dir: Path | None = None) -> bool:
    """Whether the Beta switch is on (the hub's own .env is loaded into the
    environment at start, the deployment file arrives the same way)."""
    from .settings import truthy

    return truthy(os.environ.get(ENV) or "")


def source(hub_dir: Path) -> str:
    """Where the agent's files come from: ``image`` (a separate data folder,
    the files are baked in), ``git`` (a checkout) or ``local`` (a folder)."""
    from . import datadir

    hub_dir = Path(hub_dir).resolve()
    if datadir.hub_read_only(hub_dir):
        return "image"
    for folder in (hub_dir, *hub_dir.parents):
        if (folder / ".git").exists():
            return "git"
    return "local"


def allowed(hub_dir: Path) -> bool:
    """Whether the browser may change this agent's files right now."""
    return enabled(hub_dir) and source(hub_dir) != "image"


def message(hub_dir: Path, what: str = "this agent's files") -> str:
    """Why the browser can't change ``what``, and where to change it instead."""
    tail = {"image": "Change them in Git, then redeploy the image.",
            "git": "Change them in Git, then deploy."}.get(source(hub_dir), "Edit the files, or ask Claude Code or Codex.")
    return f"You can't change {what} here. {tail}"


def refusal(hub_dir: Path, what: str = "this agent's files", *, always: bool = False) -> dict | None:
    """None when allowed, else the error body every route returns (409).
    ``always``: allowed with the switch off too, unless the files come from an image."""
    where = source(hub_dir)
    if allowed(hub_dir) or (always and where != "image"):
        return None
    # Files from an image keep 2.0.1's code: the switch would not help there.
    return {"code": "hub_read_only" if where == "image" else CODE, "message": message(hub_dir, what), "source": where}


def check(hub_dir: Path, what: str = "this agent's files", *, always: bool = False) -> None:
    """Raise the 409 a browser route returns when the change isn't allowed."""
    body = refusal(hub_dir, what, always=always)
    if body is not None:
        from .control.http import error

        raise error(409, body["code"], body["message"], source=body["source"])


def status(hub_dir: Path) -> dict:
    """What the web app shows: whether it may offer editors, and why not."""
    return {"in_app_editing": enabled(hub_dir), "editable": allowed(hub_dir),
            "files_from": source(hub_dir), "message": None if allowed(hub_dir) else message(hub_dir)}
