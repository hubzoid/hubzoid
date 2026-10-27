"""The Claude Code plugin in plugins/hubzoid-builder ships the builder skill.

skills/builder/SKILL.md stays the source: the website's setup prompt and the
docs link to it on main. The plugin carries an exact copy, because Anthropic's
directory and plugin installs read only the plugin folder.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "hubzoid-builder"


def test_plugin_skill_matches_builder_skill():
    source = (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")
    copy = (PLUGIN / "skills" / "hubzoid-builder" / "SKILL.md").read_text(encoding="utf-8")
    assert copy == source, (
        "Copy skills/builder/SKILL.md to "
        "plugins/hubzoid-builder/skills/hubzoid-builder/SKILL.md"
    )


def test_marketplace_points_at_the_plugin_folder():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"] == "hubzoid-builder"
    assert (ROOT / entry["source"]).resolve() == PLUGIN


def test_plugin_folder_has_readme_and_license():
    assert len((PLUGIN / "README.md").read_text().split()) >= 40
    assert (PLUGIN / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()
