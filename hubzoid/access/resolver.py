# Hubzoid Enterprise · access management.
# Source-available and free to run for development and testing; production use
# requires a license key with the "access" entitlement. See LICENSING.md.
# This is a notice, not a gate: the feature runs on the community tier too.
"""Resolve a surface-native handle (a WhatsApp/Telegram phone) into the hub's
canonical identity: ``{email, groups, ...context}``.

This is the generic identity seam every non-web surface plugs into. It mirrors
the ``restricted/`` convention: a hub opts in by dropping a file in the
``identity/`` folder, presence-activated, owner-authored, git-committed. Two
backings, one contract ``resolve(surface, handle) -> {email, groups} | None``:

  * ``identity/access.py`` — a function ``resolve(surface, handle)`` (live CRM /
    API lookup). Wins if present.
  * ``identity/access.csv`` — a table ``phone,email[,groups][,extra]`` the
    operator edits (all rows in one file). Keyed on the normalized phone.

Email is the key; every downstream check (Open WebUI group resolution, the
access guard) already keys on it. ``None`` means the handle is unknown, which
on a webhook surface means fail-closed: the roster is the allowlist.
"""
from __future__ import annotations

import csv
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Callable

from .._fs import resolve_bucket
from ..inbound.normalize import normalize_phone

log = logging.getLogger("hubzoid.access")

# A resolver turns (surface, handle) into an identity record or None.
Resolver = Callable[[str, str], "dict | None"]

_CSV_NAME = "access.csv"
_PY_NAME = "access.py"
_GROUP_SPLIT = re.compile(r"[;,]")


def load_resolver(hub_dir) -> "Resolver | None":
    """Return a resolver callable, or ``None`` if the hub has no ``identity/``.

    Built once (the adapter calls this at startup and reuses the callable, so
    the table is parsed / the module imported a single time). A ``.py`` function
    backing wins over a ``.csv`` table. ``None`` return = no directory at all;
    on a webhook surface that means every sender is unknown -> rejected.
    """
    idir = resolve_bucket(Path(hub_dir), "identity")
    if idir is None:
        return None
    py = idir / _PY_NAME
    table = idir / _CSV_NAME
    if py.is_file():
        _notice_license()
        return _function_resolver(py)
    if table.is_file():
        _notice_license()
        return _table_resolver(table)
    return None


# ---------------------------------------------------------------------------
# Table backing
# ---------------------------------------------------------------------------
def _table_resolver(path: Path) -> "Resolver":
    index = _load_table(path)
    log.info("identity: loaded %d row(s) from %s", len(index), path)

    def resolver(surface: str, handle: str) -> "dict | None":
        key = normalize_phone(handle)
        if not key:
            return None
        return _copy(index.get(key))

    return resolver


def _load_table(path: Path) -> "dict[str, dict]":
    """Parse the CSV into ``normalized_phone -> record``.

    Headers are lowercased + trimmed; the phone is normalized so a prettified
    cell still matches a raw ``wa_id``; the email is lowercased to match Open
    WebUI; groups split on ``;`` or ``,``; blank rows skipped; any other column
    is preserved on the record as context (center, name, …).
    """
    index: "dict[str, dict]" = {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except OSError:
        log.warning("identity: could not read %s", path, exc_info=True)
        return index
    if not rows:
        return index

    header = [c.strip().lower() for c in rows[0]]
    for raw in rows[1:]:
        if not any((cell or "").strip() for cell in raw):
            continue  # blank row
        cells = {
            header[i]: (raw[i].strip() if i < len(raw) else "")
            for i in range(len(header))
        }
        phone = normalize_phone(cells.get("phone"))
        if not phone:
            continue  # no key -> unusable row
        record: dict = {
            "email": (cells.get("email") or "").strip().lower() or None,
            "groups": _norm_groups(cells.get("groups")),
        }
        for k, v in cells.items():
            if k in ("phone", "email", "groups"):
                continue
            if v:
                record[k] = v
        index[phone] = record
    return index


# ---------------------------------------------------------------------------
# Function backing
# ---------------------------------------------------------------------------
def _function_resolver(path: Path) -> "Resolver":
    mod = _load_module(path)
    fn = getattr(mod, "resolve", None)
    if not callable(fn):
        log.error(
            "identity/%s defines no resolve(surface, handle) function; "
            "all senders will be unknown (fail-closed).", path.name,
        )
        return lambda surface, handle: None

    def resolver(surface: str, handle: str) -> "dict | None":
        try:
            out = fn(surface, handle)
        except Exception:  # noqa: BLE001 — a resolver bug denies, never crashes the surface
            log.exception("identity resolve() raised (surface=%s); denying", surface)
            return None
        return _normalize_record(out)

    return resolver


def _load_module(path: Path):
    """Import a .py file as a uniquely-named module (mirrors access/loader.py)."""
    mod_name = f"hubzoid_identity_{path.stem}_{abs(hash(path.as_posix())) % 10_000_000}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Record shaping (shared by both backings)
# ---------------------------------------------------------------------------
def _normalize_record(rec) -> "dict | None":
    """Coerce a resolver's return into the canonical record, or None."""
    if not rec or not isinstance(rec, dict):
        return None
    out = dict(rec)
    email = out.get("email")
    if isinstance(email, str):
        out["email"] = email.strip().lower() or None
    out["groups"] = _norm_groups(out.get("groups"))
    return out


def _norm_groups(g) -> "list[str]":
    if g is None:
        return []
    parts = _GROUP_SPLIT.split(g) if isinstance(g, str) else list(g)
    return [str(p).strip() for p in parts if str(p).strip()]


def _copy(rec) -> "dict | None":
    """A fresh copy so callers can't mutate the shared table index."""
    if rec is None:
        return None
    out = dict(rec)
    out["groups"] = list(rec.get("groups") or [])
    return out


def _notice_license() -> None:
    """Inform (never block) that identity resolution is an Enterprise feature.

    Same soft open-core posture as `access.guard`: runs fully on the community
    tier; a valid ``access`` license silences this. Change the warning to a
    raise here to switch from informing to enforcing.
    """
    from .. import licensing

    if not licensing.load_license().has_feature("access"):
        log.warning(
            "Identity resolution (identity/) is a Hubzoid Enterprise feature; "
            "production use needs a license. See LICENSING.md."
        )
