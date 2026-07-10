from pathlib import Path

import pytest

from hubzoid.loaders import agents as agents_loader
from hubzoid.loaders import knowledge as knowledge_loader
from hubzoid.loaders import skills as skills_loader
from hubzoid.loaders import tools_local as tools_local_loader

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


def test_load_main_agent():
    loaded = agents_loader.load_main(MINIMAL)
    assert loaded.spec.name == "testbot"
    assert "TestBot" in loaded.instructions


def test_load_subagents():
    subs = agents_loader.load_subagents(MINIMAL)
    assert len(subs) == 1
    assert subs[0].spec.name == "echo"
    assert subs[0].spec.tools == ["reverse_string"]


def test_load_skills():
    skills = skills_loader.load_all(MINIMAL)
    names = {s.spec.name: s for s in skills}
    # The hub's own skill…
    assert "greet" in names
    assert "Hello" in names["greet"].body
    # …plus the core-shipped skills every hub gets (e.g. dashboard).
    assert "dashboard" in names


def test_load_skills_hub_only():
    """Hub-authored skills, without the core-shipped ones."""
    hub_only = skills_loader._scan_dir(
        __import__("hubzoid._fs", fromlist=["resolve_bucket"]).resolve_bucket(MINIMAL, "skills")
    )
    assert [s.spec.name for s in hub_only] == ["greet"]


def test_load_knowledge():
    kn = knowledge_loader.load_all(MINIMAL)
    assert len(kn) == 1
    assert kn[0].name == "colors"
    assert "red" in kn[0].body
    assert "primary" in kn[0].keywords


def test_load_tools_local():
    tools = tools_local_loader.load_all(MINIMAL)
    assert "reverse_string" in tools
    assert "sentinel_marker" in tools


def test_missing_AGENTS_md_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        agents_loader.load_main(tmp_path)


def test_load_main_with_no_body_raises(tmp_path):
    (tmp_path / "AGENTS.md").write_text("---\nname: x\ndescription: y\n---\n")
    with pytest.raises(ValueError, match="body"):
        agents_loader.load_main(tmp_path)


def test_load_main_no_frontmatter_uses_folder_name(tmp_path):
    """Plain markdown with no frontmatter should still load."""
    hub = tmp_path / "demo-hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("You are a helpful assistant. Be concise.")
    loaded = agents_loader.load_main(hub)
    assert loaded.spec.name == "demo-hub"
    assert "helpful assistant" in loaded.spec.description


def test_load_main_partial_frontmatter_fills_defaults(tmp_path):
    """Frontmatter with just `name` should still work; description derived."""
    hub = tmp_path / "demo"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: pickme\n---\n# Heading\n\nThe body line.")
    loaded = agents_loader.load_main(hub)
    assert loaded.spec.name == "pickme"
    assert loaded.spec.description == "The body line."


def test_load_subagent_no_frontmatter_uses_folder_name(tmp_path):
    """Sub-agent AGENTS.md without frontmatter falls back to folder name."""
    hub = tmp_path / "hub"
    (hub / "agents" / "scout").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("main agent body")
    (hub / "agents" / "scout" / "AGENTS.md").write_text("You are the scout sub-agent.")
    subs = agents_loader.load_subagents(hub)
    assert len(subs) == 1
    assert subs[0].spec.name == "scout"


def test_load_subagent_flat_md_layout(tmp_path):
    """`agents/<name>.md` (flat) should load just like the folder layout."""
    hub = tmp_path / "hub"
    (hub / "agents").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("main")
    (hub / "agents" / "scribe.md").write_text(
        "---\nname: scribe\ndescription: writes things down\n---\n"
        "You are the scribe."
    )
    subs = agents_loader.load_subagents(hub)
    assert len(subs) == 1
    assert subs[0].spec.name == "scribe"
    assert "You are the scribe" in subs[0].instructions


def test_load_subagent_flat_md_without_frontmatter(tmp_path):
    """Flat agents/foo.md with no frontmatter falls back to the file stem."""
    hub = tmp_path / "hub"
    (hub / "agents").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("main")
    (hub / "agents" / "echo-flat.md").write_text("You just echo.")
    subs = agents_loader.load_subagents(hub)
    assert len(subs) == 1
    assert subs[0].spec.name == "echo-flat"


def test_load_subagent_mixed_flat_and_folder_layouts(tmp_path):
    """Both layouts can coexist in the same agents/ folder."""
    hub = tmp_path / "hub"
    (hub / "agents" / "beta").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("main")
    (hub / "agents" / "alpha.md").write_text(
        "---\nname: alpha\ndescription: flat one\n---\nA"
    )
    (hub / "agents" / "beta" / "AGENTS.md").write_text(
        "---\nname: beta\ndescription: folder one\n---\nB"
    )
    subs = agents_loader.load_subagents(hub)
    names = sorted(s.spec.name for s in subs)
    assert names == ["alpha", "beta"]


def test_flat_agent_promoted_to_skill(tmp_path):
    """A flat agents/<name>.md should also be promoted to the skill registry."""
    hub = tmp_path / "hub"
    (hub / "agents").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("main")
    (hub / "agents" / "summary.md").write_text(
        "---\nname: summary\ndescription: summarizer\n---\nSummarize."
    )
    skills = agents_loader.promote_to_skills(hub)
    assert len(skills) == 1
    assert skills[0].spec.name == "summary"
    assert "Summarize" in skills[0].body


def test_split_subagents_no_model_is_skill():
    # minimal_hub's `echo` sub-agent has no model -> skill bucket, empty delegates.
    skills, delegates = agents_loader.split_subagents(MINIMAL, "claude-local")
    assert [a.spec.name for a in skills] == ["echo"]
    assert delegates == []


def test_split_subagents_different_tier_is_delegate(tmp_path):
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        "---\nname: opusguy\ndescription: hard stuff\nmodel: claude-local/opus\n"
        "tools: [read_file]\n---\nYou are the opus specialist."
    )
    skills, delegates = agents_loader.split_subagents(tmp_path, "claude-local")
    assert skills == []
    assert [a.spec.name for a in delegates] == ["opusguy"]
    assert delegates[0].spec.model == "claude-local/opus"
    assert delegates[0].spec.tools == ["read_file"]


def test_split_subagents_none_hub_model_all_skills(tmp_path):
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        "---\nname: opusguy\ndescription: d\nmodel: claude-local/opus\n---\nbody"
    )
    skills, delegates = agents_loader.split_subagents(tmp_path, None)
    assert [a.spec.name for a in skills] == ["opusguy"]
    assert delegates == []
