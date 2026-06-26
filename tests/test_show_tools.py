"""SHOW_TOOLS: how tool-call activity is surfaced.

compact (default) -> collapsible dropdown on web, hidden on Slack.
full              -> legacy inline blockquote on every surface.
off               -> emit nothing.

Mirrors the SHOW_THINKING knob (see test_thinking_indicator.py).
"""
from __future__ import annotations

from hubzoid import reasoning
from hubzoid import settings as settingslib


# --- normalize_tools: off | compact | full, default compact ----------------
def test_normalize_tools_default_is_compact():
    assert reasoning.normalize_tools(None) == "compact"
    assert reasoning.normalize_tools("") == "compact"


def test_normalize_tools_passes_canonical_values():
    assert reasoning.normalize_tools("off") == "off"
    assert reasoning.normalize_tools("compact") == "compact"
    assert reasoning.normalize_tools("full") == "full"


def test_normalize_tools_is_case_insensitive():
    assert reasoning.normalize_tools("  COMPACT ") == "compact"


def test_normalize_tools_off_aliases():
    for alias in ("false", "none", "no", "0", "hide", "hidden", "disabled"):
        assert reasoning.normalize_tools(alias) == "off"


def test_normalize_tools_full_aliases():
    for alias in ("inline", "legacy", "blockquote", "verbose"):
        assert reasoning.normalize_tools(alias) == "full"


def test_normalize_tools_unknown_falls_back_to_compact():
    assert reasoning.normalize_tools("banana") == "compact"


# --- settings.load wires SHOW_TOOLS onto Settings.show_tools ---------------
def test_settings_default_show_tools_is_compact(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOW_TOOLS", raising=False)
    s = settingslib.load(tmp_path)
    assert s.show_tools == "compact"


def test_settings_reads_show_tools_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOW_TOOLS", "off")
    s = settingslib.load(tmp_path)
    assert s.show_tools == "off"
