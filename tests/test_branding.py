"""Tests for the per-hub branding folder convention."""
from __future__ import annotations

from pathlib import Path

import pytest

from hubzoid import branding


def _write(p: Path, content: str = "<svg/>") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# find_slot_file: case insensitivity + extension preference
# ---------------------------------------------------------------------------
def test_finds_lowercase_svg(tmp_path):
    branding_dir = tmp_path / "branding"
    _write(branding_dir / "logo.svg")
    assert branding.find_slot_file(branding_dir, "logo").name == "logo.svg"


def test_case_insensitive_filename(tmp_path):
    branding_dir = tmp_path / "branding"
    _write(branding_dir / "LOGO.PNG")
    assert branding.find_slot_file(branding_dir, "logo").name == "LOGO.PNG"


def test_case_insensitive_mixed(tmp_path):
    branding_dir = tmp_path / "branding"
    _write(branding_dir / "Favicon.SVG")
    assert branding.find_slot_file(branding_dir, "favicon").name == "Favicon.SVG"


def test_extension_preference_svg_over_png(tmp_path):
    """When both svg and png exist, svg wins (first in the accepted list)."""
    branding_dir = tmp_path / "branding"
    _write(branding_dir / "logo.svg")
    _write(branding_dir / "logo.png")
    assert branding.find_slot_file(branding_dir, "logo").suffix == ".svg"


def test_missing_slot_returns_none(tmp_path):
    branding_dir = tmp_path / "branding"
    branding_dir.mkdir()
    assert branding.find_slot_file(branding_dir, "splash") is None


def test_no_branding_dir_returns_none(tmp_path):
    assert branding.find_slot_file(tmp_path / "branding", "logo") is None


def test_unrelated_files_ignored(tmp_path):
    branding_dir = tmp_path / "branding"
    _write(branding_dir / "README.md", "# branding")
    _write(branding_dir / "notes.txt", "hi")
    assert branding.find_slot_file(branding_dir, "logo") is None


# ---------------------------------------------------------------------------
# apply: copies files into both static dirs, preserves extensions
# ---------------------------------------------------------------------------
def test_apply_copies_logo_to_favicon_target(tmp_path):
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.svg", "<svg id=logo/>")
    static.mkdir()

    applied = branding.apply(hub, static)

    assert "favicon.svg" in applied  # logo.* now aliases to favicon.<ext>
    # Both roots: static_dir and static_dir/static
    assert (static / "favicon.svg").read_text() == "<svg id=logo/>"
    assert (static / "static" / "favicon.svg").read_text() == "<svg id=logo/>"


def test_apply_favicon_overrides_logo(tmp_path):
    """If both logo and favicon exist, favicon wins (processed after logo)."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.svg", "<svg id=logo/>")
    _write(hub / "branding" / "favicon.svg", "<svg id=favicon/>")
    static.mkdir()

    branding.apply(hub, static)

    assert (static / "favicon.svg").read_text() == "<svg id=favicon/>"


def test_apply_preserves_extension(tmp_path):
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.png", "fake-png")
    static.mkdir()

    branding.apply(hub, static)

    assert (static / "favicon.png").read_text() == "fake-png"
    assert not (static / "favicon.svg").exists()


def test_apply_splash_target(tmp_path):
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "splash.png", "splash-bytes")
    static.mkdir()

    applied = branding.apply(hub, static)

    assert "splash.png" in applied  # canonical-filename key, not slot name
    assert (static / "splash.png").read_text() == "splash-bytes"
    assert (static / "static" / "splash.png").read_text() == "splash-bytes"


def test_apply_no_branding_dir_writes_only_baseline_css(tmp_path):
    """No branding/ folder => only the baseline custom.css is written."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    hub.mkdir()
    static.mkdir()

    applied = branding.apply(hub, static)

    # Baseline CSS lands even with no branding folder.
    assert applied == {"custom.css": "<baseline>"}
    css_top = (static / "custom.css").read_text()
    css_nested = (static / "static" / "custom.css").read_text()
    assert "Workspace" in css_top
    assert "Voice mode" in css_top
    assert "Voice Input" in css_top
    assert css_top == css_nested


def test_apply_accepts_custom_baseline_css(tmp_path):
    """apply() takes a baseline_css override so the gateway can keep the
    Workspace tab (its admins manage groups/ACLs there) while single-hub
    keeps the default. A hub-supplied custom.css still wins over it."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    hub.mkdir()
    static.mkdir()

    branding.apply(hub, static, baseline_css="/* gateway css */")

    assert (static / "custom.css").read_text() == "/* gateway css */"
    assert (static / "static" / "custom.css").read_text() == "/* gateway css */"


def test_gateway_baseline_css_keeps_workspace_hides_voice():
    """The gateway baseline must NOT hide Workspace (admins need it to manage
    per-team groups + model ACLs), but still hides the unreliable voice/mic
    buttons like the single-hub baseline does."""
    css = branding.GATEWAY_BASELINE_CSS
    # No Workspace-hiding selectors (the word may appear in an explanatory
    # comment; what matters is that neither hide rule is present).
    assert 'a[href="/workspace"]' not in css
    assert 'aria-label="Workspace"' not in css
    # Voice + mic are still hidden, same as the single-hub baseline.
    assert 'aria-label="Voice mode"' in css
    assert 'aria-label="Voice Input"' in css
    # Sanity: the single-hub baseline DOES hide Workspace, proving they differ.
    assert 'a[href="/workspace"]' in branding._BASELINE_CUSTOM_CSS


def test_apply_custom_css_override_wins(tmp_path):
    """A hub-supplied custom.css replaces the baseline entirely."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "custom.css", "body { background: red; }")
    static.mkdir()

    applied = branding.apply(hub, static)

    assert applied["custom.css"] != "<baseline>"
    assert (static / "custom.css").read_text() == "body { background: red; }"
    assert "Workspace" not in (static / "custom.css").read_text()


def test_baseline_css_hides_target_selectors():
    """The baseline must hide at least Workspace, Voice mode, and Voice Input."""
    css = branding.baseline_custom_css()
    assert 'a[href="/workspace"]' in css
    assert 'button[aria-label="Voice mode"]' in css
    assert 'button[aria-label="Voice Input"]' in css


def test_apply_idempotent(tmp_path):
    """Running apply twice produces the same end state."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.svg", "<svg id=v1/>")
    static.mkdir()

    branding.apply(hub, static)
    # Now edit the source and re-apply: new content should land.
    _write(hub / "branding" / "logo.svg", "<svg id=v2/>")
    branding.apply(hub, static)

    assert (static / "favicon.svg").read_text() == "<svg id=v2/>"


# ---------------------------------------------------------------------------
# single-master derivation: one image brands every raster slot
# ---------------------------------------------------------------------------
def test_apply_single_favicon_derives_all_raster_slots(tmp_path):
    """A lone favicon.png fills every raster chrome slot (light + dark, sidebar,
    splash, PWA), so one image brands the whole UI. .svg/.ico are NOT faked."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "favicon.png", "MASTER")
    static.mkdir()

    branding.apply(hub, static)

    for name in (
        "favicon.png", "favicon-dark.png", "favicon-96x96.png",
        "apple-touch-icon.png", "logo.png", "splash.png", "splash-dark.png",
        "web-app-manifest-192x192.png", "web-app-manifest-512x512.png",
    ):
        assert (static / name).read_text() == "MASTER", name
        assert (static / "static" / name).read_text() == "MASTER", name
    # PNG bytes are never written as svg/ico (wrong format).
    assert not (static / "favicon.svg").exists()
    assert not (static / "favicon.ico").exists()


def test_apply_logo_png_seeds_derivation_via_favicon_alias(tmp_path):
    """logo.png with no favicon.png aliases to favicon.png AND seeds derivation
    of every other raster slot."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.png", "LOGO")
    static.mkdir()

    branding.apply(hub, static)

    assert (static / "favicon.png").read_text() == "LOGO"
    assert (static / "favicon-dark.png").read_text() == "LOGO"
    assert (static / "splash.png").read_text() == "LOGO"


def test_apply_explicit_variant_wins_over_derivation(tmp_path):
    """Supplied variants are kept; only the missing slots are synthesized.
    splash-dark derives from a supplied splash.png, not the bare favicon."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "favicon.png", "LIGHT")
    _write(hub / "branding" / "favicon-dark.png", "DARK")
    _write(hub / "branding" / "splash.png", "SPLASH")
    static.mkdir()

    branding.apply(hub, static)

    assert (static / "favicon-dark.png").read_text() == "DARK"    # kept
    assert (static / "splash.png").read_text() == "SPLASH"        # kept
    assert (static / "splash-dark.png").read_text() == "SPLASH"   # from splash
    assert (static / "logo.png").read_text() == "LIGHT"           # from master


def test_apply_svg_only_does_not_fabricate_png_slots(tmp_path):
    """An svg-only branding dir must not synthesize png slots from svg bytes."""
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    _write(hub / "branding" / "logo.svg", "<svg/>")
    static.mkdir()

    applied = branding.apply(hub, static)

    assert "favicon.svg" in applied
    assert not (static / "favicon.png").exists()
    assert not (static / "splash.png").exists()


def test_has_assets(tmp_path):
    """has_assets ignores README/notes and missing dirs, sees real assets."""
    empty = tmp_path / "b1"
    _write(empty / "README.md", "# branding")
    assert branding.has_assets(empty) is False
    assert branding.has_assets(tmp_path / "does-not-exist") is False

    good = tmp_path / "b2"
    _write(good / "favicon.png", "x")
    assert branding.has_assets(good) is True
