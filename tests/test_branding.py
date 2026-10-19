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
def test_has_assets(tmp_path):
    """has_assets ignores README/notes and missing dirs, sees real assets."""
    empty = tmp_path / "b1"
    _write(empty / "README.md", "# branding")
    assert branding.has_assets(empty) is False
    assert branding.has_assets(tmp_path / "does-not-exist") is False

    good = tmp_path / "b2"
    _write(good / "favicon.png", "x")
    assert branding.has_assets(good) is True

