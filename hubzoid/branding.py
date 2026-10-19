"""The ``<hub>/branding/`` folder convention: which files count as brand assets.

A deployment's page chrome comes from a branding folder (the gateway's, or a
hub's). ``has_assets`` says whether a folder holds any, so the gateway can fall
back to the next candidate. Matching is case-insensitive; ``logo.<ext>`` is a
synonym for ``favicon.<ext>``, and one ``favicon.png`` fills every raster slot
it does not supply. The web app serves these files at ``/branding/*`` and the
reviewed brand manifest (``hubzoid.ui.branding_*``) decides what is shown.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("hubzoid.branding")

# Exact filenames (lowercased) that count as brand assets.
# Case-insensitive on the input; output uses the canonical case below.
_MIRROR_NAMES: list[str] = [
    "favicon.svg",
    "favicon.png",
    "favicon-dark.png",
    "favicon-96x96.png",
    "favicon.ico",
    "apple-touch-icon.png",
    "logo.png",
    "splash.png",
    "splash-dark.png",
    "web-app-manifest-192x192.png",
    "web-app-manifest-512x512.png",
    "custom.css",
]

# Raster slots synthesized from the master (favicon.png) when the operator did
# not supply them, so a single image brands every surface. `.svg`
# and `.ico` are deliberately absent — PNG bytes are not valid in those formats.
_DERIVE_FROM_MASTER: tuple[str, ...] = (
    "favicon-dark.png",
    "favicon-96x96.png",
    "apple-touch-icon.png",
    "logo.png",
    "splash.png",
    "splash-dark.png",
    "web-app-manifest-192x192.png",
    "web-app-manifest-512x512.png",
)


# Logo aliases: branding/logo.<ext> is treated as branding/favicon.<ext>.
# Only honored when the corresponding favicon.<ext> file is NOT also
# present in branding/ (favicon wins on tie).
_LOGO_TO_FAVICON_EXTS = {"svg", "png", "webp", "jpg", "jpeg"}


def _build_source_index(branding_dir: Path) -> dict[str, Path]:
    """Index files in branding_dir by canonical asset filename.

    Case-insensitive match on both stem and extension. Logo aliases are
    folded into favicon.* slots if favicon.* is absent.
    """
    if not branding_dir.is_dir():
        return {}

    files = {entry.name.lower(): entry for entry in branding_dir.iterdir() if entry.is_file()}
    index: dict[str, Path] = {}

    # 1. Direct matches.
    for canonical in _MIRROR_NAMES:
        match = files.get(canonical)
        if match is not None:
            index[canonical] = match

    # 2. Logo aliases. logo.svg -> favicon.svg, logo.png -> favicon.png, etc.
    # Only when the favicon.<ext> direct match isn't already present.
    for ext in _LOGO_TO_FAVICON_EXTS:
        logo = files.get(f"logo.{ext}")
        if logo is None:
            continue
        target = f"favicon.{ext}"
        if target in _MIRROR_NAMES and target not in index:
            index[target] = logo

    # 3. Single-master derivation. Any raster slot the operator did not supply
    # is filled from favicon.png (aliased from logo.png above when needed), so
    # one image brands every surface. splash-dark prefers a supplied splash.png
    # over the bare favicon. Only PNG slots are synthesized (see _MIRROR_NAMES).
    if "splash.png" in index:
        index.setdefault("splash-dark.png", index["splash.png"])
    master = index.get("favicon.png")
    if master is not None:
        for name in _DERIVE_FROM_MASTER:
            index.setdefault(name, master)

    return index


def has_assets(branding_dir: Path) -> bool:
    """True when ``branding_dir`` yields at least one brand asset.

    Ignores non-asset files (README, notes). Used to decide whether an
    explicit gateway branding dir is populated before falling back to a hub's.
    """
    return bool(_build_source_index(branding_dir))


# Backwards-compatible function (used by older tests). Returns the source
# file for a "slot" name, mirroring the previous slot-based API.
def find_slot_file(branding_dir: Path, slot: str) -> Path | None:
    """Find a branding file for a generic ``slot`` name. Case-insensitive.

    Slot is matched against canonical filenames where the stem equals
    ``slot``. For ``slot='logo'``, also accepts ``logo.<ext>`` files
    directly (the logo->favicon alias applies only to the asset index).

    Returns the first matching file, with extension preference following
    the legacy ordering: svg, png, webp, jpg, jpeg, ico.
    """
    if not branding_dir.is_dir():
        return None
    files = {entry.name.lower(): entry for entry in branding_dir.iterdir() if entry.is_file()}
    slot_lower = slot.lower()
    for ext in ("svg", "png", "webp", "jpg", "jpeg", "ico"):
        match = files.get(f"{slot_lower}.{ext}")
        if match is not None:
            return match
    return None
