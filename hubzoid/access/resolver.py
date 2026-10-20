# Hubzoid access management. Apache-2.0, like the rest of the repo (see LICENSING.md).
"""Resolve identity for a hub, from either surface-native handle or email.

This is the generic identity seam every non-web surface plugs into. First the
phone numbers recorded on people in the Console (``GrantStore.set_phone``): a
WhatsApp sender, or a Telegram sender who shared their contact, with that
number is that person. Then, when the hub has one, its own lookup code,
``identity/access.py``: a function ``resolve(surface, handle)`` (live CRM / API
lookup). It MAY also define ``groups_for_email(email)`` to describe people on
the web/MCP surfaces; a file that defines only ``resolve`` is never handed an
email. Groups describe people and grant nothing.

``identity/access.csv`` is no longer read (one home for each fact: phone numbers
are on people in Settings, and who may use a channel is a grant there). A hub
that still has one gets a warning naming what to do instead.

``Roster`` stays callable — ``roster(surface, handle) -> {email, groups} | None``
— so the inbound harness and Telegram enrollment are unchanged. On a webhook
surface the handle lookup is the allowlist (unknown -> None -> rejected).
"""
from __future__ import annotations

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

_CSV_NAME = "access.csv"   # no longer read; named so doctor and logs can say so
_CSV_WARNED: set[str] = set()
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


def load_resolver(hub_dir) -> "Roster":
    """The hub's ``Roster``: the Console's phone numbers, then the hub's own
    ``identity/`` roster when it has one (``hub_roster``)."""
    return _ConsolePhones(hub_dir, hub_roster(hub_dir))


class _ConsolePhones(Roster):
    """A sender whose number is recorded on a person in the Console is that
    person. Anyone else is asked of the hub's own roster, if any. A blocked
    person is nobody. A store error asks the roster only (fail closed for the
    Console's part)."""

    def __init__(self, hub_dir, fallback: "Roster | None"):
        self._hub_dir = Path(hub_dir)
        self.fallback = fallback

    def __call__(self, surface: str, handle: str) -> "dict | None":
        try:
            from . import store_for

            gs = store_for(self._hub_dir)
            subject = gs.subject_for_phone(handle)
            if subject and not gs.is_suspended(subject):
                return {"email": subject, "groups": self.groups_for_email(subject)}
        except Exception:  # noqa: BLE001 — the roster still answers
            log.warning("identity: Console phone lookup failed; asking the hub roster only")
        return self.fallback(surface, handle) if self.fallback is not None else None

    def groups_for_email(self, email: str | None) -> "list[str]":
        return self.fallback.groups_for_email(email) if self.fallback is not None else []


def hub_roster(hub_dir) -> "Roster | None":
    """The hub's own roster from ``identity/``, or ``None`` without one.

    The CSV backing reloads itself on file change, so a single instance stays
    current."""
    idir = resolve_bucket(Path(hub_dir), "identity")
    if idir is None:
        return None
    py = idir / _PY_NAME
    if py.is_file():
        return _FunctionRoster(py)
    if (idir / _CSV_NAME).is_file() and str(idir) not in _CSV_WARNED:
        _CSV_WARNED.add(str(idir))
        log.warning("identity/%s in %s is no longer read: record people's phone numbers in Settings, and "
                    "grant who may use a channel there. A hub's own identity/access.py still works.",
                    _CSV_NAME, Path(hub_dir).name)
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


