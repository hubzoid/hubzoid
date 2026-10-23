"""A hub argument given as a deployment folder.

Most commands take ``HUB`` and default to the current folder. In the folder that
holds ``hubzoid.yaml`` (several hubs, no ``AGENTS.md``) that used to reach code
that expects a hub and fail with a traceback. Here, once for the whole CLI:

- a deployment of one hub means that hub;
- account and backup commands (``admin``, ``backup``) work on the whole
  deployment, so they take its first hub (they read the shared store);
- any other command asks which hub, naming them.
"""
from __future__ import annotations

from pathlib import Path

import typer

#: Commands whose work spans the deployment: any hub of it gives the same result.
DEPLOYMENT_WIDE = frozenset({"admin", "backup"})
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


def resolve(value: Path, *, command: str = "", deployment_wide: bool = False) -> Path:
    """The hub ``value`` stands for (see the module docstring)."""
    folder = Path(value)
    hubs = _hubs(folder.resolve()) if folder.is_dir() else None
    if not hubs:
        return value
    if len(hubs) == 1 or deployment_wide:
        return hubs[0]
    names = ", ".join(h.name for h in hubs)
    example = f"hubzoid {command} {hubs[0].name}".replace("  ", " ") if command else f"... {hubs[0].name}"
    raise typer.BadParameter(f"This folder runs {len(hubs)} hubs ({names}). Name one, for example: {example}")


def _callback(path: tuple[str, ...], previous):
    wide = bool(path) and path[0] in DEPLOYMENT_WIDE
    command = " ".join(path)

    def callback(ctx, param, value):
        if previous is not None:
            value = previous(ctx, param, value)
        if value is None:
            return value
        return resolve(Path(value), command=command, deployment_wide=wide)

    return callback


def _patch(cmd, path: tuple[str, ...]) -> None:
    # Typer ships its own copy of Click, so match by shape, not by class.
    for param in cmd.params:
        if getattr(param, "param_type_name", "") == "argument" and param.name in _NAMES and str(param.default) == ".":
            if getattr(param, "_hz_hubarg", False):
                continue
            param.callback = _callback(path, param.callback)
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
