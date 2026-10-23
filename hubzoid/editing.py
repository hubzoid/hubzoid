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
and stay editable.
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
    where = source(hub_dir)
    if where == "image":
        return (f"You can't change {what} here: this agent's files come with its image. "
                "Change them in Git and redeploy.")
    if where == "git":
        tail = "Change them in your repository (or ask Claude Code or Codex), then deploy."
    else:
        tail = "Edit the agent's files on this computer, or ask Claude Code or Codex."
    return f"Changing {what} from the browser is off. {tail}"


def refusal(hub_dir: Path, what: str = "this agent's files") -> dict | None:
    """None when allowed, else the error body every route returns (409)."""
    if allowed(hub_dir):
        return None
    return {"code": CODE, "message": message(hub_dir, what), "source": source(hub_dir)}


def check(hub_dir: Path, what: str = "this agent's files") -> None:
    """Raise the 409 a browser route returns when the change isn't allowed."""
    body = refusal(hub_dir, what)
    if body is not None:
        from .control.http import error

        raise error(409, CODE, body["message"], source=body["source"])


def status(hub_dir: Path) -> dict:
    """What the web app shows: whether it may offer editors, and why not."""
    return {"in_app_editing": enabled(hub_dir), "editable": allowed(hub_dir),
            "files_from": source(hub_dir), "message": None if allowed(hub_dir) else message(hub_dir)}
