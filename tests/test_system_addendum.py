"""Tests for the auto-injected system-prompt addendum."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hubzoid import system_addendum
from hubzoid.loaders.knowledge import LoadedKnowledge
from hubzoid.loaders.skills import LoadedSkill, SkillSpec


@dataclass
class _Ctx:
    """Minimal HubContext stand-in. Only fields the addendum reads are set."""
    hub_dir: Path
    settings: Any
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    output_dir: Path = None
    session_id: str = "test"


class _S:
    """Minimal Settings stand-in."""
    def __init__(self, model: str | None = None):
        self.model = model


def _skill(name: str, description: str = "desc") -> LoadedSkill:
    return LoadedSkill(spec=SkillSpec(name=name, description=description),
                       body="b", source_path=Path("/tmp/x"))


def _kn(name: str, description: str = "kn desc") -> LoadedKnowledge:
    return LoadedKnowledge(name=name, description=description, body="b")


def test_addendum_header_and_environment(tmp_path):
    ctx = _Ctx(hub_dir=tmp_path / "my-hub", settings=_S("claude-local/haiku"))
    ctx.hub_dir.mkdir()
    out = system_addendum.build(ctx, backend="claude-local")
    assert "Hubzoid runtime context" in out
    assert "## Environment" in out
    assert "Hub name: my-hub" in out
    assert "Backend: claude-local" in out
    assert "Model: claude-local/haiku" in out
    # Today's date is rendered as YYYY-MM-DD.
    assert "Today's date" in out


def test_addendum_omits_knowledge_section_when_empty(tmp_path):
    ctx = _Ctx(hub_dir=tmp_path, settings=_S(), skills=[], knowledge=[])
    out = system_addendum.build(ctx, backend="openai-agents")
    assert "## Knowledge available" not in out
    assert "## Skills available" not in out
    # But the generic tool guidance always appears.
    assert "## How to use your tools" in out


def test_addendum_lists_knowledge_when_present(tmp_path):
    ctx = _Ctx(
        hub_dir=tmp_path,
        settings=_S(),
        knowledge=[_kn("jexl-expressions", "JEXL in PRS"), _kn("flow-steps", "flow primitives")],
    )
    out = system_addendum.build(ctx, backend="claude-local")
    assert "## Knowledge available" in out
    assert "- jexl-expressions: JEXL in PRS" in out
    assert "- flow-steps: flow primitives" in out
    # And nudges towards calling the tool.
    assert "read_knowledge" in out


def test_addendum_lists_skills_when_present(tmp_path):
    ctx = _Ctx(
        hub_dir=tmp_path,
        settings=_S(),
        skills=[_skill("collect-fields", "interview"), _skill("test-template", "QA")],
    )
    out = system_addendum.build(ctx, backend="claude-local")
    assert "## Skills available" in out
    assert "- collect-fields: interview" in out
    assert "- test-template: QA" in out
    assert "load_skill" in out


def test_tools_section_is_generic_not_domain_specific(tmp_path):
    """The 'How to use your tools' block must NOT reference specific tools."""
    ctx = _Ctx(hub_dir=tmp_path, settings=_S(),
               skills=[_skill("a")], knowledge=[_kn("b")])
    out = system_addendum.build(ctx, backend="claude-local")
    start = out.index("## How to use your tools")
    section = out[start:]
    # Generic behavioural guidance only.
    assert "parallel" in section
    # No mention of any specific tool name in the guidance section.
    for tool_name in ("read_knowledge", "load_skill", "write_artifact",
                      "read_upload", "list_files", "render_jinja"):
        assert tool_name not in section, f"{tool_name!r} leaked into the generic section"


def test_addendum_includes_uploads_section(tmp_path):
    """User-uploaded files (Slack attachments, OWUI uploads) are reachable
    only via read_upload. The agent must be told this — otherwise it tries
    to read them via Bash / Read / subagent (Claude Code-style escape) on
    hallucinated paths under ~/.claude/projects/."""
    ctx = _Ctx(hub_dir=tmp_path / "h", settings=_S("claude-local/haiku"))
    ctx.hub_dir.mkdir()
    out = system_addendum.build(ctx, backend="claude-local")
    assert "## Reading user-uploaded files" in out
    upload_idx = out.index("## Reading user-uploaded files")
    next_section = out.find("\n## ", upload_idx + 1)
    section = out[upload_idx:next_section if next_section != -1 else None]
    # Names the right tools.
    assert "read_upload" in section
    assert "read_upload_full" in section
    # Explicitly forbids the escape routes the agent reaches for.
    for forbidden in ("Bash", "Read", "subagent"):
        assert forbidden in section, f"{forbidden!r} not warned about"
    # Tells the model how to paginate.
    assert "offset" in section


def test_uploads_section_comes_before_generic_tools_section(tmp_path):
    """test_tools_section_is_generic_not_domain_specific slices from
    '## How to use your tools' to end-of-string and asserts no specific
    tool names appear. Uploads section must therefore come BEFORE the
    generic section so its read_upload mention doesn't leak in."""
    ctx = _Ctx(hub_dir=tmp_path / "h", settings=_S())
    ctx.hub_dir.mkdir()
    out = system_addendum.build(ctx, backend="claude-local")
    assert out.index("## Reading user-uploaded files") < out.index("## How to use your tools")


def test_is_enabled_default_true(tmp_path):
    (tmp_path / "AGENTS.md").write_text("---\nname: x\ndescription: y\n---\nbody")
    assert system_addendum.is_enabled(tmp_path) is True


def test_is_enabled_false_via_frontmatter(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: x\ndescription: y\nauto_addendum: false\n---\nbody"
    )
    assert system_addendum.is_enabled(tmp_path) is False


@pytest.mark.parametrize("raw", ["no", "0", "off", "False"])
def test_is_enabled_string_falsey_variants(tmp_path, raw):
    (tmp_path / "AGENTS.md").write_text(
        f"---\nname: x\ndescription: y\nauto_addendum: {raw}\n---\nbody"
    )
    assert system_addendum.is_enabled(tmp_path) is False


def test_is_enabled_when_no_agents_md(tmp_path):
    # If there's no main AGENTS.md, return True (the build will fail later
    # anyway; addendum opt-out is irrelevant).
    assert system_addendum.is_enabled(tmp_path) is True


# ---------------------------------------------------------------------------
# raw_data section
# ---------------------------------------------------------------------------
def test_raw_data_section_omitted_when_folder_absent(tmp_path):
    ctx = _Ctx(hub_dir=tmp_path, settings=_S())
    out = system_addendum.build(ctx, backend="openai-agents")
    assert "Searching raw_data" not in out


def test_raw_data_section_present_when_folder_exists(tmp_path):
    (tmp_path / "raw_data").mkdir()
    ctx = _Ctx(hub_dir=tmp_path, settings=_S())
    out = system_addendum.build(ctx, backend="openai-agents")
    assert "## Searching raw_data/" in out
    assert "grep_data" in out
    assert "list_files" in out


def test_raw_data_section_present_for_hyphen_variant(tmp_path):
    (tmp_path / "raw-data").mkdir()
    ctx = _Ctx(hub_dir=tmp_path, settings=_S())
    out = system_addendum.build(ctx, backend="openai-agents")
    assert "## Searching raw_data/" in out
