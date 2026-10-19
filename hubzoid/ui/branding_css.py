"""Decorative brand CSS: a deliberately tiny subset.

A brand may decorate exactly three slots the app exports,
``[data-brand-slot="brand.logoFrame"]``, ``[data-brand-slot="brand.welcome"]``
and ``[data-brand-slot="brand.footer"]``, with four properties:
``background-color`` and ``border-color`` (six-digit hex), ``border-radius``
(0 to 24 px) and ``padding`` (one to four lengths, 0 to 32 px). Nothing else
parses: no other selectors, descendants, pseudo-elements, at-rules, URLs,
custom properties, positioning, display, visibility or generated content.
No slot wraps approval cards, errors, model identity or permission notices,
so decoration can't hide or imitate them.

The grammar is small enough that a strict hand-written parser is safer than a
general CSS parser plus a filter: anything it doesn't recognise is an error.
"""
from __future__ import annotations

import re

from .branding_models import BrandError

SLOTS = ("brand.logoFrame", "brand.welcome", "brand.footer")
MAX_BYTES = 8 * 1024

_RULE = re.compile(r'\[data-brand-slot="(?P<slot>[A-Za-z.]+)"\]\s*\{(?P<body>[^{}]*)\}')
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PX = re.compile(r"^(0|[0-9]{1,2}px)$")


def _px(value: str, top: int) -> bool:
    if not _PX.match(value):
        return False
    return int(value.removesuffix("px") or 0) <= top


def parse(text: str, where: str = "decorative_css") -> dict[str, dict[str, str]]:
    """slot -> {property: value}. Raises ``BrandError(unsafe_style)``."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise BrandError(413, "asset_too_large", f"{where} is larger than 8 KiB.", pointer=where)
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out: dict[str, dict[str, str]] = {}
    pos = 0
    for match in _RULE.finditer(stripped):
        if stripped[pos:match.start()].strip():
            raise _bad(where, "only rules for the brand slots are allowed")
        pos = match.end()
        slot = match.group("slot")
        if slot not in SLOTS:
            raise _bad(where, f"{slot!r} is not a brand slot ({', '.join(SLOTS)})")
        decls = out.setdefault(slot, {})
        for decl in match.group("body").split(";"):
            if not decl.strip():
                continue
            if ":" not in decl:
                raise _bad(where, f"{decl.strip()!r} is not a declaration")
            prop, value = (p.strip() for p in decl.split(":", 1))
            prop = prop.lower()
            if "!" in value:
                raise _bad(where, "!important is not allowed")
            if prop in ("background-color", "border-color"):
                if not _HEX.match(value):
                    raise _bad(where, f"{prop} must be a six-digit hex colour")
            elif prop == "border-radius":
                if not _px(value, 24):
                    raise _bad(where, "border-radius is 0 to 24px")
            elif prop == "padding":
                parts = value.split()
                if not 1 <= len(parts) <= 4 or not all(_px(p, 32) for p in parts):
                    raise _bad(where, "padding is one to four lengths of 0 to 32px")
            else:
                raise _bad(where, f"{prop} is not allowed (only background-color, border-color, "
                                  "border-radius and padding)")
            decls[prop] = value
    if stripped[pos:].strip():
        raise _bad(where, "only rules for the brand slots are allowed")
    return out


def render(rules: dict[str, dict[str, str]]) -> str:
    """Canonical CSS from parsed rules (what is actually served)."""
    lines = []
    for slot in SLOTS:
        decls = rules.get(slot)
        if decls:
            body = "; ".join(f"{k}: {v}" for k, v in sorted(decls.items()))
            lines.append(f'[data-brand-slot="{slot}"] {{ {body}; }}')
    return "\n".join(lines) + ("\n" if lines else "")


def _bad(where: str, why: str) -> BrandError:
    return BrandError(422, "unsafe_style", f"{where}: {why}.", pointer=where)
