"""The Claude Code plugin in plugins/hubzoid ships the builder skill as build-hub.

skills/builder/SKILL.md stays the source: the website's setup prompt and the
docs link to it on main. The plugin carries an exact copy, because Anthropic's
directory and plugin installs read only the plugin folder.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "hubzoid"


def test_plugin_skill_matches_builder_skill():
    source = (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")
    copy = (PLUGIN / "skills" / "build-hub" / "SKILL.md").read_text(encoding="utf-8")
    assert copy == source, (
        "Copy skills/builder/SKILL.md to "
        "plugins/hubzoid/skills/build-hub/SKILL.md"
    )


def test_marketplace_points_at_the_plugin_folder():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"] == "hubzoid"
    assert (ROOT / entry["source"]).resolve() == PLUGIN


def test_plugin_folder_has_readme_and_license():
    assert len((PLUGIN / "README.md").read_text().split()) >= 40
    assert (PLUGIN / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()


def test_openai_manifest_matches_the_claude_manifest():
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    openai = json.loads((PLUGIN / "plugin.json").read_text())
    assert openai["name"] == claude["name"]
    ui = openai["extensions"]["com.openai"]["interface"]
    assert len(ui["displayName"]) <= 30 and len(ui["shortDescription"]) <= 30
    for key in ("composerIcon", "logo"):
        assert (PLUGIN / ui[key]).is_file()
    assert (PLUGIN / openai["extensions"]["com.openai"]["onboardingSkill"]).is_file()
