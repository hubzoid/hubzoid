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
    assert len(skills) == 1
    assert skills[0].spec.name == "greet"
    assert "Hello" in skills[0].body


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
