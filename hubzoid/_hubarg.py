"""A hub argument given as a deployment folder.

Most commands take ``HUB`` and default to the current folder. In the folder that
holds ``hubzoid.yaml`` (several hubs, no ``AGENTS.md``) that used to reach code
that expects a hub and fail with a traceback. Here, once for the whole CLI:

- a deployment of one hub means that hub;
- account, backup, brand and embedded-app commands (``admin``, ``backup``,
  ``branding``, ``embed``) work on the whole deployment, so they take its
  first hub (they read the shared store and ``hubzoid.yaml``);
- any other command asks which hub, naming them.
"""
from __future__ import annotations

from pathlib import Path

import typer

#: Commands whose work spans the deployment: any hub of it gives the same result.
DEPLOYMENT_WIDE = frozenset({"admin", "backup", "embed", "branding"})
_NAMES = ("hub", "hub_dir")


def _hubs(folder: Path) -> list[Path] | None:
    """The hubs of the deployment in ``folder``, or None when it is not one."""
    from . import deployfile

    if (folder / "AGENTS.md").is_file():
        return None
    path = deployfile.find(folder)
    if path is None:
        return None
    try:
        return deployfile.load(path).hub_dirs()
    except deployfile.DeployFileError:
        return None


def resolve(value: Path, *, command: str = "", deployment_wide: bool = False, flag: str = "") -> Path:
    """The hub ``value`` stands for (see the module docstring)."""
    folder = Path(value)
    hubs = _hubs(folder.resolve()) if folder.is_dir() else None
    if not hubs:
        return value
    if len(hubs) == 1 or deployment_wide:
        return hubs[0]
    names = ", ".join(h.name for h in hubs)
    named = f"{flag} {hubs[0].name}" if flag else hubs[0].name
    example = f"hubzoid {command} {named}".replace("  ", " ") if command else f"... {named}"
    raise typer.BadParameter(f"This folder runs {len(hubs)} hubs ({names}). Name one, for example: {example}")


def deployment_environment(hub: Path) -> None:
    """Fill in what the hub's hubzoid.yaml sets (public_url, sign_in, owner,
    in_app_editing) as `hubzoid run` would, for a command run on its own.
    The process environment still wins; a broken file changes nothing."""
    import os

    from . import deployfile

    try:
        path = deployfile.owning(Path(hub))
        if path is None:
            return
        values, _conflicts = deployfile.environment(deployfile.load(path))
    except (deployfile.DeployFileError, OSError, ValueError):
        return
    for name, value in values.items():
        os.environ.setdefault(name, value)


def _callback(path: tuple[str, ...], previous, flag: str = ""):
    wide = bool(path) and path[0] in DEPLOYMENT_WIDE
    command = " ".join(path)

    def callback(ctx, param, value):
        if previous is not None:
            value = previous(ctx, param, value)
        if value is None:
            return value
        hub = resolve(Path(value), command=command, deployment_wide=wide, flag=flag)
        deployment_environment(hub)   # e.g. public_url for links and webhooks
        return hub

    return callback


def _patch(cmd, path: tuple[str, ...]) -> None:
    # Typer ships its own copy of Click, so match by shape, not by class.
    for param in cmd.params:
        kind = getattr(param, "param_type_name", "")
        if kind in ("argument", "option") and param.name in _NAMES and str(param.default) == ".":
            if getattr(param, "_hz_hubarg", False):
                continue
            flag = (param.opts[0] if param.opts else "") if kind == "option" else ""
            param.callback = _callback(path, param.callback, flag)
            param._hz_hubarg = True  # noqa: SLF001 - our own marker
    for name, sub in (getattr(cmd, "commands", None) or {}).items():
        _patch(sub, path + (name,))


class HubArgGroup(typer.core.TyperGroup):
    """The root group: patches every sub-command's hub argument once."""

    def get_command(self, ctx, cmd_name: str):
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            _patch(cmd, (cmd_name,))
        return cmd
