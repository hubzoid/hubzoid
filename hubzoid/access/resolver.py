# Hubzoid Enterprise · access management.
# Source-available and free to run for development and testing; production use
# requires a license key with the "access" entitlement. See LICENSING.md.
# This is a notice, not a gate: the feature runs on the community tier too.
"""Resolve identity for a hub, from either surface-native handle or email.

This is the generic identity seam every non-web surface plugs into. A hub opts
in by dropping a file in the ``identity/`` folder, presence-activated,
owner-authored, git-committed. Two backings, one object (``Roster``):

  * ``identity/access.py`` — a function ``resolve(surface, handle)`` (live CRM /
    API lookup). Wins if present. It MAY also define ``groups_for_email(email)``
    to opt in to email-keyed lookups on the web/MCP surfaces; a legacy file that
    defines only ``resolve`` is never handed an email.
  * ``identity/access.csv`` — a table ``phone,email[,groups][,extra]`` the
    operator edits. Keyed on the normalized phone AND on the lowercased email,
    so the same person resolves from a WhatsApp number or an Open WebUI login.

``Roster`` stays callable — ``roster(surface, handle) -> {email, groups} | None``
— so the inbound harness and Telegram enrollment are unchanged. New:
``roster.groups_for_email(email) -> list[str]`` (``[]`` for unknown), which the
bridge unions into a web/MCP caller's Open WebUI groups. On that path the roster
is ADDITIVE, never a gate: an email absent from it simply contributes nothing,
so OWUI-only users are never locked out. On a webhook surface the handle lookup
is still the allowlist (unknown -> None -> rejected).

The CSV backing reloads when the file changes on disk (mtime/size/inode stamp),
so an operator edit takes effect on the next lookup with no restart. It is
fail-closed by construction: an unreadable or half-saved CSV resolves to no
groups (deny), never to a stale last-known-good grant. Save atomically
(write-then-rename) to avoid torn reads.
"""
from __future__ import annotations

import csv
import importlib.util
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .._fs import resolve_bucket
from ..inbound.normalize import normalize_phone

log = logging.getLogger("hubzoid.access")

# A resolver turns (surface, handle) into an identity record or None. A Roster
# satisfies this (it is callable), so existing type hints keep holding.
Resolver = Callable[[str, str], "dict | None"]

_CSV_NAME = "access.csv"
_PY_NAME = "access.py"
_GROUP_SPLIT = re.compile(r"[;,]")

# Cache a resolved live-lookup for a short window so a web request never sits on
# a fresh CRM round-trip on every message. Bounded so a direct API client can't
# grow it without limit by varying the email.
_PY_CACHE_TTL_SECONDS = 60.0
_PY_CACHE_MAX = 4096


class Roster:
    """Callable identity backing for a hub. Subclasses provide the two lookups.

    ``__call__(surface, handle)`` keeps the historical resolver contract.
    ``groups_for_email(email)`` is the new web/MCP path; the base returns no
    groups so a backing that cannot answer by email is safe by default.
    """

    def __call__(self, surface: str, handle: str) -> "dict | None":
        raise NotImplementedError

    def groups_for_email(self, email: str | None) -> "list[str]":
        return []


def load_resolver(hub_dir) -> "Roster | None":
    """Return a ``Roster`` for the hub, or ``None`` if it has no ``identity/``.

    Built once per hub (see ``roster_for``); the CSV backing reloads itself on
    file change, so a single instance stays current. A ``.py`` backing wins over
    a ``.csv`` table. ``None`` = no directory at all; on a webhook surface that
    means every sender is unknown -> rejected.
    """
    idir = resolve_bucket(Path(hub_dir), "identity")
    if idir is None:
        return None
    py = idir / _PY_NAME
    table = idir / _CSV_NAME
    if py.is_file():
        _notice_license()
        return _FunctionRoster(py)
    if table.is_file():
        _notice_license()
        return _CsvRoster(table)
    return None


# ---------------------------------------------------------------------------
# Per-hub accessor (used by the bridge and MCP, which hold no roster of their
# own and must not reparse per request).
# ---------------------------------------------------------------------------
_ROSTER_CACHE: "dict[str, Roster | None]" = {}
_ROSTER_LOCK = threading.Lock()


def roster_for(hub_dir) -> "Roster | None":
    """The hub's ``Roster``, built once and reused. Fail-closed on any load or
    import error: returns ``None`` (no roster groups) rather than raising, so a
    broken ``identity/`` denies instead of 500-ing a request."""
    key = str(Path(hub_dir).resolve())
    with _ROSTER_LOCK:
        if key in _ROSTER_CACHE:
            return _ROSTER_CACHE[key]
        try:
            roster = load_resolver(hub_dir)
        except Exception:  # noqa: BLE001 — a broken backing denies, never crashes
            log.exception("identity: roster load failed for %s; denying", hub_dir)
            roster = None
        _ROSTER_CACHE[key] = roster
        return roster


def reset_roster_cache() -> None:
    """Drop the per-hub roster cache (tests, or an ``identity/`` swap)."""
    with _ROSTER_LOCK:
        _ROSTER_CACHE.clear()


# ---------------------------------------------------------------------------
# CSV backing
# ---------------------------------------------------------------------------
class _Snapshot:
    """One immutable parse of the CSV: both indexes plus the file stamp they
    were read at. Swapped atomically on reload."""

    __slots__ = ("by_phone", "by_email", "stamp")

    def __init__(self, by_phone, by_email, stamp):
        self.by_phone = by_phone
        self.by_email = by_email
        self.stamp = stamp


class _CsvRoster(Roster):
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._snap = self._reload()

    def _stat_stamp(self):
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _current(self) -> "_Snapshot":
        """Return the live snapshot, reparsing if the file changed on disk."""
        stamp = self._stat_stamp()
        snap = self._snap
        if stamp == snap.stamp:
            return snap
        with self._lock:
            if stamp == self._snap.stamp:  # someone else reloaded while we waited
                return self._snap
            self._snap = self._reload(stamp)
            return self._snap

    def _reload(self, stamp=None) -> "_Snapshot":
        if stamp is None:
            stamp = self._stat_stamp()
        by_phone, by_email = _load_table(self._path)
        log.info(
            "identity: loaded %d phone / %d email row(s) from %s",
            len(by_phone), len(by_email), self._path,
        )
        return _Snapshot(by_phone, by_email, stamp)

    def __call__(self, surface: str, handle: str) -> "dict | None":
        key = normalize_phone(handle)
        if not key:
            return None
        return _copy(self._current().by_phone.get(key))

    def groups_for_email(self, email: str | None) -> "list[str]":
        key = (email or "").strip().lower()
        if not key:
            return []
        return list(self._current().by_email.get(key, ()))


def _load_table(path: Path) -> "tuple[dict[str, dict], dict[str, list]]":
    """Parse the CSV into ``(phone -> record, email -> [groups])``.

    Headers are lowercased + trimmed; the phone is normalized so a prettified
    cell still matches a raw ``wa_id``; the email is lowercased to match Open
    WebUI; groups split on ``;`` or ``,``; blank rows skipped; any other column
    is preserved on the phone record as context (center, name, …). Rows that
    share an email UNION their groups (one person, several numbers is
    legitimate). Fail-closed: any read error yields two empty indexes.
    """
    by_phone: "dict[str, dict]" = {}
    by_email: "dict[str, set]" = {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except OSError:
        log.warning("identity: could not read %s; denying", path, exc_info=True)
        return {}, {}
    if not rows:
        return {}, {}

    header = [c.strip().lower() for c in rows[0]]
    for raw in rows[1:]:
        if not any((cell or "").strip() for cell in raw):
            continue  # blank row
        cells = {
            header[i]: (raw[i].strip() if i < len(raw) else "")
            for i in range(len(header))
        }
        phone = normalize_phone(cells.get("phone"))
        email = (cells.get("email") or "").strip().lower() or None
        groups = _norm_groups(cells.get("groups"))
        if phone:
            record: dict = {"email": email, "groups": groups}
            for k, v in cells.items():
                if k in ("phone", "email", "groups"):
                    continue
                if v:
                    record[k] = v
            by_phone[phone] = record
        if email:
            by_email.setdefault(email, set()).update(groups)
    return by_phone, {e: sorted(g) for e, g in by_email.items()}


# ---------------------------------------------------------------------------
# Function backing
# ---------------------------------------------------------------------------
class _FunctionRoster(Roster):
    def __init__(self, path: Path):
        mod = _load_module(path)
        self._path = path
        resolve = getattr(mod, "resolve", None)
        self._resolve = resolve if callable(resolve) else None
        gfe = getattr(mod, "groups_for_email", None)
        self._groups_for_email = gfe if callable(gfe) else None
        if self._resolve is None:
            log.error(
                "identity/%s defines no resolve(surface, handle) function; "
                "all senders will be unknown (fail-closed).", path.name,
            )
        self._cache: "dict[str, tuple[float, list]]" = {}
        self._cache_lock = threading.Lock()

    def __call__(self, surface: str, handle: str) -> "dict | None":
        if self._resolve is None:
            return None
        try:
            out = self._resolve(surface, handle)
        except Exception:  # noqa: BLE001 — a resolver bug denies, never crashes the surface
            log.exception("identity resolve() raised (surface=%s); denying", surface)
            return None
        return _normalize_record(out)

    def groups_for_email(self, email: str | None) -> "list[str]":
        # Opt-in only: a legacy access.py that defines only resolve() is NEVER
        # handed an email (it may ignore its args and hand out a fixed group).
        if self._groups_for_email is None:
            return []
        key = (email or "").strip().lower()
        if not key:
            return []
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < _PY_CACHE_TTL_SECONDS:
                return list(hit[1])
        try:
            groups = _norm_groups(self._groups_for_email(key))
        except Exception:  # noqa: BLE001 — a lookup bug denies, never crashes
            log.exception("identity groups_for_email() raised; denying")
            groups = []
        with self._cache_lock:
            if len(self._cache) >= _PY_CACHE_MAX:
                self._cache.clear()  # crude bound; correctness over churn
            self._cache[key] = (now, list(groups))
        return list(groups)


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
