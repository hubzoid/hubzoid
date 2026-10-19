"""Where a hub keeps what it writes.

By default a hub writes under its own folder: ``.hubzoid/`` (databases, keys,
chats, artifacts, evals, outbox), ``output/``, ``logs/`` and ``.inbound/``.

With ``HUBZOID_DATA_DIR`` set, the same layout lives under that folder instead
and the hub folder is only read. That is how a hosted hub runs: its own files
are baked into the image from Git, and one durable volume (``/data``) holds its
state. One hub per data folder, run on its own: a gateway refuses it.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV = "HUBZOID_DATA_DIR"
STATE = ".hubzoid"
# What a hub writes, relative to its data root (backups copy these).
WRITTEN = (STATE, ".inbound", "logs", "output")


def configured(env=None) -> Path | None:
    """The data folder named by ``HUBZOID_DATA_DIR``, or None."""
    value = ((os.environ if env is None else env).get(ENV) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def root(hub_dir: Path, env=None) -> Path:
    """The folder a hub's state lives under: the data folder, or the hub."""
    return configured(env) or Path(hub_dir)


def state(hub_dir: Path, env=None) -> Path:
    """The hub's ``.hubzoid`` folder: databases, keys, chats, artifacts."""
    return root(hub_dir, env) / STATE


def resolve(hub_dir: Path, rel: str | Path, env=None) -> Path:
    """A hub-relative path, where it really is: under the data root when it
    starts with one of the folders a hub writes (``.hubzoid/...``,
    ``output/...``), else under the hub folder."""
    rel = Path(rel)
    if rel.parts and rel.parts[0] in WRITTEN:
        return root(hub_dir, env) / rel
    return Path(hub_dir) / rel


def roots(hub_dir: Path, env=None) -> list[Path]:
    """The folders a hub's paths may live under: the hub, and its data folder
    when that is separate. Containment checks accept either."""
    hub = Path(hub_dir).resolve()
    data = configured(env)
    return [hub] if data is None or data == hub else [hub, data]


READ_ONLY = "This agent's files come from its Git repository. Change them there and redeploy."


def hub_read_only(hub_dir: Path, env=None) -> bool:
    """True when the hub's own files come from its image: state lives in a
    separate data folder, so an edit to the hub folder would not survive a
    redeploy. Change those files in Git instead."""
    data = configured(env)
    return data is not None and data != Path(hub_dir).resolve()
