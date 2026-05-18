"""Per-hub branding: copy <hub>/branding/<file> into Open WebUI's static dir.

Convention: drop logo, favicon, and splash files into `<hub>/branding/`.
Filename match is case-insensitive and accepts any of several extensions
per slot. Open WebUI's defaults render when a slot is unset.

This module runs on every `hubzoid run` before the OWUI subprocess starts.
Idempotent: it overwrites the static dir each time so a re-themed hub is
picked up on the next start.

Open WebUI's "brand mark" is the favicon. There is no separate logo URL.
We support a `logo.*` filename as a convenience (most operators think
"logo" when they want to swap the mark) and treat it as the favicon. If
both `logo.*` and `favicon.*` exist, favicon wins.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("hubzoid.branding")

# Slot name -> ordered list of accepted extensions (preferred first).
# Match wins on the first ext found case-insensitively.
SLOTS: dict[str, list[str]] = {
    "logo": ["svg", "png", "webp", "jpg", "jpeg"],
    "favicon": ["svg", "ico", "png"],
    "splash": ["png", "svg", "webp", "jpg", "jpeg"],
}

# Process order. Favicon comes after logo so it overrides at the same target.
_PROCESS_ORDER = ["logo", "favicon", "splash"]


def find_slot_file(branding_dir: Path, slot: str) -> Path | None:
    """Find a branding file for `slot` in `branding_dir`. Case-insensitive.

    Examples that all match slot='logo':
        logo.svg   LOGO.SVG   Logo.png   logo.WEBP
    """
    if not branding_dir.is_dir():
        return None
    accepted = SLOTS.get(slot, [])
    by_lower_name = {entry.name.lower(): entry for entry in branding_dir.iterdir() if entry.is_file()}
    slot_lower = slot.lower()
    for ext in accepted:
        match = by_lower_name.get(f"{slot_lower}.{ext}")
        if match is not None:
            return match
    return None


def apply(hub_dir: Path, static_dir: Path) -> dict[str, Path]:
    """Copy hub branding into Open WebUI's static dir. Idempotent.

    Returns a dict of {slot: source_path} for slots that were applied.
    Slots with no source file are silently skipped.

    `static_dir` is Open WebUI's static-asset root. We write to both
    `static_dir` and `static_dir/static` because OWUI's asset routing
    has dual conventions across versions.
    """
    branding_dir = hub_dir / "branding"
    if not branding_dir.is_dir():
        return {}

    applied: dict[str, Path] = {}
    for slot in _PROCESS_ORDER:
        src = find_slot_file(branding_dir, slot)
        if src is None:
            continue
        ext = src.suffix.lower()  # e.g. ".svg", includes the dot

        # Both logo and favicon target the favicon slot in OWUI; splash
        # has its own. Source extension is preserved on the destination.
        if slot in ("logo", "favicon"):
            target_stem = "favicon"
        else:
            target_stem = "splash"

        destinations = [
            static_dir / f"{target_stem}{ext}",
            static_dir / "static" / f"{target_stem}{ext}",
        ]
        for dst in destinations:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        applied[slot] = src
        log.info("branding: %s -> %s%s (+ static/)", src.name, target_stem, ext)
    return applied


def static_dir() -> Path | None:
    """Locate Open WebUI's installed static directory.

    Returns None if open_webui is not importable in this environment.
    """
    try:
        import open_webui  # type: ignore
    except ImportError:
        return None
    pkg_dir = Path(open_webui.__file__).resolve().parent
    candidate = pkg_dir / "static"
    return candidate if candidate.is_dir() else None
