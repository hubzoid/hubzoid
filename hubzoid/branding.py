"""Per-hub branding: copy <hub>/branding/<file> into Open WebUI's static dirs.

Convention: drop branding files into ``<hub>/branding/``. Filenames map
1:1 to OWUI asset filenames. Match is case-insensitive. Each branding
file is copied (with its case-corrected name) into every Open WebUI
static root we know about. If a file is absent, OWUI's default for that
asset renders.

Known OWUI asset filenames (the set hubzoid will mirror):
  favicon.svg
  favicon.png
  favicon-96x96.png
  favicon.ico
  apple-touch-icon.png
  splash.png
  splash-dark.png

Convenience aliases: ``logo.{svg,png,...}`` in branding/ is treated as a
synonym for ``favicon.<same-ext>`` (most operators say "logo" when they
mean the brand mark; OWUI's brand mark is the favicon).

Idempotent. Runs on every ``hubzoid run`` before the OWUI subprocess
starts. Edits to branding/ are picked up on next start.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("hubzoid.branding")

# Exact filenames (lowercased) that we mirror to OWUI's static dirs.
# Case-insensitive on the input; output uses the canonical case below.
_MIRROR_NAMES: list[str] = [
    "favicon.svg",
    "favicon.png",
    "favicon-96x96.png",
    "favicon.ico",
    "apple-touch-icon.png",
    "splash.png",
    "splash-dark.png",
    "custom.css",
]


# Baseline CSS hubzoid injects into Open WebUI's `<static>/custom.css`
# slot. OWUI's index.html already references this file, so just writing
# bytes to the path is enough — no template work.
#
# Why CSS and not env vars: OWUI has no env vars to hide the Workspace
# nav from admins, no toggle to hide the voice-mode / mic / read-aloud
# buttons (Issue #12771 open). CSS is the working route.
#
# Operator can override by dropping their own `custom.css` into
# `<hub>/branding/`; it replaces this baseline entirely.
_BASELINE_CUSTOM_CSS = """\
/* hubzoid baseline overrides. Override by placing your own
   custom.css in <hub>/branding/. */

/* Hide the Workspace nav item from the sidebar (admin and non-admin).
   Hubzoid agents are independent products; the Workspace tab exposes
   OWUI-as-platform surfaces that should not be visible to chat users. */
a[href="/workspace"],
a[aria-label="Workspace"] {
  display: none !important;
}

/* Hide the in-chat voice-mode button (full-duplex call).
   The feature is unreliable (OWUI Issue #22684) and confuses users. */
button[aria-label="Voice mode"] {
  display: none !important;
}

/* Hide the mic / voice-input button.
   Browser STT quality is uneven and the button often does nothing on
   first click while permissions resolve. Disable until reliable. */
button[aria-label="Voice Input"] {
  display: none !important;
}
"""


def baseline_custom_css() -> str:
    """The hubzoid baseline custom.css. Exposed for tests."""
    return _BASELINE_CUSTOM_CSS

# Logo aliases: branding/logo.<ext> is treated as branding/favicon.<ext>.
# Only honored when the corresponding favicon.<ext> file is NOT also
# present in branding/ (favicon wins on tie).
_LOGO_TO_FAVICON_EXTS = {"svg", "png", "webp", "jpg", "jpeg"}


def _build_source_index(branding_dir: Path) -> dict[str, Path]:
    """Index files in branding_dir by canonical OWUI filename.

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

    return index


def apply(hub_dir: Path, static_dir: Path) -> dict[str, Path | str]:
    """Copy hub branding into one OWUI static root. Idempotent.

    Returns a dict of {canonical_filename: source_path | "<baseline>"}
    for assets that were written. Missing files are silently skipped
    (OWUI defaults render).

    Files are written to BOTH ``static_dir/<name>`` and
    ``static_dir/static/<name>`` because OWUI's asset routing has both
    layouts across versions.

    A baseline ``custom.css`` is always written even when the hub has
    no ``branding/`` folder; this is how hubzoid hides Workspace, voice
    mode, and mic buttons (no OWUI env vars exist for these). Operators
    override by placing their own ``custom.css`` in ``<hub>/branding/``.
    """
    branding_dir = hub_dir / "branding"
    index = _build_source_index(branding_dir)

    applied: dict[str, Path | str] = {}
    for canonical, src in index.items():
        for dst in (static_dir / canonical, static_dir / "static" / canonical):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        applied[canonical] = src
        log.info("branding: %s -> %s", src.name, canonical)

    # Baseline custom.css. Only written when the hub did not provide its
    # own (per-hub custom.css already landed in the loop above).
    if "custom.css" not in applied:
        for dst in (static_dir / "custom.css", static_dir / "static" / "custom.css"):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(_BASELINE_CUSTOM_CSS)
        applied["custom.css"] = "<baseline>"
        log.info("branding: baseline custom.css -> custom.css")

    return applied


def static_dirs() -> list[Path]:
    """Locate Open WebUI's installed static directories.

    Open WebUI ships TWO static roots:
      - <pkg>/frontend/ : the SPA build OWUI serves at HTTP `/`.
        Contains favicon.png, favicon.svg, the static/ subdir, etc.
      - <pkg>/static/   : PWA manifest icons (web-app-manifest-*.png).

    Returns the dirs that exist. Empty list if open_webui is not installed.
    """
    try:
        import open_webui  # type: ignore
    except ImportError:
        return []
    pkg_dir = Path(open_webui.__file__).resolve().parent
    return [d for d in (pkg_dir / "frontend", pkg_dir / "static") if d.is_dir()]


def static_dir() -> Path | None:
    """Backwards-compatible single-dir accessor. Prefers ``frontend/``."""
    dirs = static_dirs()
    return dirs[0] if dirs else None


# Backwards-compatible function (used by older tests). Returns the source
# file for a "slot" name, mirroring the previous slot-based API.
def find_slot_file(branding_dir: Path, slot: str) -> Path | None:
    """Find a branding file for a generic ``slot`` name. Case-insensitive.

    Slot is matched against canonical filenames where the stem equals
    ``slot``. For ``slot='logo'``, also accepts ``logo.<ext>`` files
    directly (the logo->favicon alias is only applied inside apply()).

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
