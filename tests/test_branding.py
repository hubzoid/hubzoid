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

    assert "logo" in applied
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

    assert "splash" in applied
    assert (static / "splash.png").read_text() == "splash-bytes"
    assert (static / "static" / "splash.png").read_text() == "splash-bytes"


def test_apply_no_branding_dir_is_noop(tmp_path):
    hub = tmp_path / "hub"
    static = tmp_path / "static"
    hub.mkdir()
    static.mkdir()

    applied = branding.apply(hub, static)

    assert applied == {}
    assert list(static.iterdir()) == []


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
