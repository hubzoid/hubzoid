"""`hubzoid.yaml`: the deployment file, the one home of deployment settings.

It lives in the deployment folder (the folder that holds the hub folders, or a
hub folder for a deployment of one) and is versioned with them::

    version: 1
    public_url: https://hubs.example.org
    sign_in: true
    owner: owner@example.org
    hubs:                 # folders, relative to this file; the first also answers at /
      - operations-hub
      - finance/finance-hub
    brand: {...}          # the brand (docs/branding.md); brand.identity.name names the deployment
    chat: {...}           # chat defaults (docs/chat-config.md)
    diagnostics: {...}    # diagnostic limits (docs/diagnostics.md)
    apps: {...}           # apps that embed the chat (docs/embedding.md)

Secrets and process settings go in `.env` next to it. `HUBZOID_PUBLIC_URL`,
`HUBZOID_AUTH` and `HUBZOID_ADMIN_EMAIL` fill a key the file leaves out (a
hosted provider may only know the public URL at deploy time); set in both and
different, the start stops and names both.

Errors name the file, the key and its line (``hubzoid.control.yamlfile``). An
invalid file stops the start: it holds policy (terms, sharing, exports, file
storage) that must never fall back to "on".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE = "hubzoid.yaml"
_KEYS = {"version", "public_url", "sign_in", "owner", "hubs", "brand", "chat", "diagnostics", "apps",
         "in_app_editing"}


class DeployFileError(ValueError):
    pass


@dataclass
class DeployFile:
    path: Path
    public_url: str | None = None
    sign_in: bool | None = None
    #: Beta: change agent files from the browser (see hubzoid.editing). Off by default.
    in_app_editing: bool | None = None
    owner: str | None = None
    hubs: list[str] = field(default_factory=list)
    brand: dict | None = None
    chat: dict | None = None
    diagnostics: dict | None = None
    apps: dict | None = None
    raw: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    def hub_dirs(self) -> list[Path]:
        return [(self.root / h).resolve() for h in self.hubs]

    @property
    def name(self) -> str | None:
        identity = (self.brand or {}).get("identity") or {}
        value = identity.get("name") if isinstance(identity, dict) else None
        return str(value).strip() or None if value else None


def find(folder: Path) -> Path | None:
    """The deployment file in ``folder``, if there is one."""
    path = Path(folder) / FILE
    return path if path.is_file() else None


def owning(hub_dir: Path) -> Path | None:
    """The deployment file of the deployment a hub belongs to: its own folder's,
    else its parent's or grandparent's when that one lists it."""
    hub_dir = Path(hub_dir).resolve()
    for folder in (hub_dir, hub_dir.parent, hub_dir.parent.parent):
        path = find(folder)
        if path is None:
            continue
        if folder == hub_dir:
            return path
        try:
            if hub_dir in load(path).hub_dirs():
                return path
        except DeployFileError:
            return path
    return None


def _fail(loaded, pointer: str, message: str) -> DeployFileError:
    line = loaded.line_of(pointer)
    where = f"{loaded.path}" + (f", line {line}" if line else "")
    return DeployFileError(f"{where}: {message}")


def load(path: Path) -> DeployFile:
    """Read and check the file. Raises DeployFileError naming the line."""
    from .control import yamlfile

    path = Path(path).resolve()
    try:
        loaded = yamlfile.load(path)
    except yamlfile.YamlError as exc:
        raise DeployFileError(f"{exc.where() or path}: {exc.message}") from exc
    data = loaded.data or {}
    if not isinstance(data, dict):
        raise DeployFileError(f"{path}: the file is a mapping of settings.")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise _fail(loaded, f"/{unknown[0]}", f"unknown key {unknown[0]!r}. Known keys: "
                                               + ", ".join(sorted(_KEYS)) + ".")
    if data.get("version") != 1:
        raise _fail(loaded, "/version", "version must be 1.")
    out = DeployFile(path=path, raw=data)
    url = data.get("public_url")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise _fail(loaded, "/public_url", "public_url is the address people open, starting "
                                               "with https:// (or http:// on this machine).")
        out.public_url = url.rstrip("/")
    if "sign_in" in data:
        if not isinstance(data["sign_in"], bool):
            raise _fail(loaded, "/sign_in", "sign_in is true or false.")
        out.sign_in = data["sign_in"]
    if "in_app_editing" in data:
        if not isinstance(data["in_app_editing"], bool):
            raise _fail(loaded, "/in_app_editing", "in_app_editing is true or false.")
        out.in_app_editing = data["in_app_editing"]
    owner = data.get("owner")
    if owner is not None:
        if not isinstance(owner, str) or "@" not in owner:
            raise _fail(loaded, "/owner", "owner is the first owner's email address.")
        out.owner = owner.strip().lower()
    hubs = data.get("hubs") or []
    if not isinstance(hubs, list) or not all(isinstance(h, str) and h.strip() for h in hubs):
        raise _fail(loaded, "/hubs", "hubs is a list of hub folders, relative to this file.")
    out.hubs = [h.strip() for h in hubs]
    for i, folder in enumerate(out.hub_dirs()):
        if not (folder / "AGENTS.md").is_file():
            raise _fail(loaded, f"/hubs/{i}", f"{out.hubs[i]} is not a hub folder (no AGENTS.md).")
    for key in ("brand", "chat", "diagnostics", "apps"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise _fail(loaded, f"/{key}", f"{key} is a mapping of settings.")
        setattr(out, key, value)
    return out


#: deployment-file key -> the environment name that can fill it
ENV_NAMES = {"public_url": "HUBZOID_PUBLIC_URL", "sign_in": "HUBZOID_AUTH", "owner": "HUBZOID_ADMIN_EMAIL",
             "in_app_editing": "HUBZOID_IN_APP_EDITING"}


def environment(df: DeployFile, env=None) -> tuple[dict[str, str], list[str]]:
    """The environment values the file sets, and the conflicts with ``env``
    (a key set in both, differently). Values are only the three settings
    above, never a secret."""
    from . import settings as settingslib

    env = os.environ if env is None else env
    values: dict[str, str] = {}
    problems: list[str] = []
    for key, name in ENV_NAMES.items():
        mine: Any = getattr(df, key)
        if mine is None:
            continue
        theirs = (env.get(name) or "").strip()
        if key in ("sign_in", "in_app_editing"):
            text = "true" if mine else "false"
            if theirs and settingslib.truthy(theirs) != mine:
                problems.append(f"{df.path} sets {key}: {text}, but {name}={theirs}. Set it in one place.")
        else:
            text = str(mine)
            if theirs and theirs.rstrip("/").lower() != text.rstrip("/").lower():
                problems.append(f"{df.path} sets {key}: {text}, but {name}={theirs}. Set it in one place.")
        values[name] = text
    return values, problems


def example(*, hubs: list[str], public_url: str | None, name: str | None, sign_in: bool | None,
            owner: str | None) -> str:
    """A deployment file that says what a `hubzoid gateway` command line said."""
    lines = ["version: 1"]
    if public_url:
        lines.append(f"public_url: {public_url}")
    if sign_in is not None:
        lines.append(f"sign_in: {'true' if sign_in else 'false'}")
    if owner:
        lines.append(f"owner: {owner}")
    lines.append("hubs:")
    lines += [f"  - {h}" for h in hubs]
    if name:
        lines += ["brand:", "  identity:", f"    name: {name}"]
    return "\n".join(lines) + "\n"
