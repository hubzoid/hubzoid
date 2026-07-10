"""Tests for #6: core-shipped skills (package resource) merged with hub skills.

  * load_core() finds the packaged `dashboard` skill.
  * load_all() exposes core skills to a hub that ships none.
  * a hub skill of the same name OVERRIDES the core one (hub wins).
  * the dashboard skill is well-formed (name/description + Chart.js + write_artifact).
"""
from __future__ import annotations

from pathlib import Path

from hubzoid.loaders import skills as skills_loader


def _names(skills):
    return {s.spec.name for s in skills}


def test_core_ships_dashboard_skill():
    core = skills_loader.load_core()
    assert "dashboard" in _names(core)


def test_load_all_includes_core_for_hub_without_skills(tmp_path):
    # hub with no skills/ dir still gets the core skills
    (tmp_path / "AGENTS.md").write_text("---\nname: A\n---\nA\n")
    skills = skills_loader.load_all(tmp_path)
    assert "dashboard" in _names(skills)


def test_hub_skill_overrides_core_by_name(tmp_path):
    sdir = tmp_path / "skills" / "dashboard"
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text(
        "---\nname: dashboard\ndescription: hub's own dashboard\n---\n\nhub body\n"
    )
    skills = skills_loader.load_all(tmp_path)
    dash = [s for s in skills if s.spec.name == "dashboard"]
    assert len(dash) == 1                       # not duplicated
    assert dash[0].spec.description == "hub's own dashboard"   # hub wins
    assert "hub body" in dash[0].body


def test_dashboard_skill_is_well_formed():
    core = {s.spec.name: s for s in skills_loader.load_core()}
    dash = core["dashboard"]
    assert dash.spec.description
    body = dash.body
    assert "write_artifact" in body            # tells the agent how to deliver
    assert "chart.js" in body.lower()          # names the library
    assert "prefers-color-scheme" in body      # light/dark guidance


def test_core_skills_dir_is_packaged():
    # the packaged dir exists on disk relative to the installed package
    d = Path(skills_loader._CORE_SKILLS_DIR)
    assert (d / "dashboard" / "SKILL.md").is_file()


def test_mcp_surface_excludes_core_skills(tmp_path):
    """Core skills (e.g. dashboard) assume the chat runtime's write_artifact,
    which the MCP surface doesn't expose — so MCP must NOT advertise them."""
    from hubzoid import factory
    (tmp_path / "AGENTS.md").write_text("---\nname: A\n---\nA\n")
    # the MCP path builds ctx.skills via _load_skills_and_promoted_agents
    mcp_skills = {s.spec.name for s in factory._load_skills_and_promoted_agents(tmp_path)}
    assert "dashboard" not in mcp_skills
    # but the chat runtime DOES get it
    chat_skills = {s.spec.name for s in factory._with_core_skills([])}
    assert "dashboard" in chat_skills
