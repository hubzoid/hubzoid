"""The bundled demo hub teaches Hubzoid, so its answers must stay current.

Two copies ship: `demo-hub/` at the repo root (folder layout for skills and
sub-agents) and `hubzoid/templates/demo/` (the `--template demo` source, flat
layout). These tests keep their teaching content aligned and guard the facts
the Guide answers from: the three runtimes and the single open-source product.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo-hub"
TEMPLATE = ROOT / "hubzoid" / "templates" / "demo"

# hub-folder-layout.md draws each copy's own tree, so it legitimately differs.
_LAYOUT_SPECIFIC = {"hub-folder-layout.md"}

pytestmark = pytest.mark.skipif(not DEMO.is_dir(), reason="demo-hub/ not present")


def _demo_markdown() -> list[Path]:
    out: list[Path] = []
    for base in (DEMO, TEMPLATE):
        out += [p for p in base.rglob("*.md") if "branding" not in p.parts]
    return out


def _without_model_line(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True)
                   if not line.startswith("model:"))


def test_knowledge_files_are_mirrored():
    demo = {p.name for p in (DEMO / "knowledge").glob("*.md")}
    template = {p.name for p in (TEMPLATE / "knowledge").glob("*.md")}
    assert demo == template
    for name in sorted(demo - _LAYOUT_SPECIFIC):
        assert (DEMO / "knowledge" / name).read_text() == (TEMPLATE / "knowledge" / name).read_text(), name


def test_skills_and_builder_are_mirrored():
    demo_skills = {p.parent.name for p in (DEMO / "skills").glob("*/SKILL.md")}
    template_skills = {p.stem for p in (TEMPLATE / "skills").glob("*.md")}
    assert demo_skills == template_skills
    for name in sorted(demo_skills):
        assert (DEMO / "skills" / name / "SKILL.md").read_text() == (TEMPLATE / "skills" / f"{name}.md").read_text(), name
    assert (DEMO / "agents" / "builder" / "AGENTS.md").read_text() == (TEMPLATE / "agents" / "builder.md").read_text()


def test_main_agent_is_mirrored_apart_from_the_pinned_model():
    """The repo demo pins `model: claude-local`; the template lets init choose."""
    assert _without_model_line((DEMO / "AGENTS.md").read_text()) == _without_model_line((TEMPLATE / "AGENTS.md").read_text())
    assert "model:" not in (TEMPLATE / "AGENTS.md").read_text().split("---")[1]


@pytest.mark.parametrize("base", [DEMO, TEMPLATE], ids=["demo-hub", "template"])
def test_what_is_hubzoid_names_all_three_runtimes(base):
    text = (base / "knowledge" / "what-is-hubzoid.md").read_text()
    for needle in ("OpenAI Agents SDK", "LiteLLM", "Claude Agent SDK", "`claude-local`",
                   "`codex-local`", "Three runtimes"):
        assert needle in text, needle
    # Current positioning: one open-source product, three experiences.
    for needle in ("Apache-2.0", "workflows", "chat", "MCP", "hubzoid.com/enterprise"):
        assert needle in text, needle


@pytest.mark.parametrize("base", [DEMO, TEMPLATE], ids=["demo-hub", "template"])
def test_guide_prompt_describes_three_runtimes(base):
    text = (base / "AGENTS.md").read_text()
    for needle in ("OpenAI Agents SDK", "Claude Agent SDK", "`claude-local`", "`codex-local`"):
        assert needle in text, needle


def test_no_stale_positioning_or_two_runtime_claims():
    stale = ("consulting practice", "six weeks", "mid-enterprise", "two engines",
             "both runtimes", "one runtime, two", "separate offerings")
    offenders = []
    for path in _demo_markdown():
        lowered = path.read_text().lower()
        offenders += [f"{path.relative_to(ROOT)}: {phrase}" for phrase in stale if phrase in lowered]
    assert offenders == []


def test_demo_markdown_keeps_the_guide_voice_free_of_long_dashes():
    offenders = [str(p.relative_to(ROOT)) for p in _demo_markdown()
                 if "\u2014" in p.read_text() or "\u2013" in p.read_text()]
    assert offenders == []


@pytest.mark.parametrize("base", [DEMO, TEMPLATE], ids=["demo-hub", "template"])
def test_both_copies_still_load(base):
    from hubzoid.loaders import agents, knowledge, skills

    assert agents.load_main(base).spec.name == "hubzoid-guide"
    assert [a.spec.name for a in agents.load_subagents(base)] == ["builder"]
    names = {k.name for k in knowledge.load_all(base)}
    assert {"what-is-hubzoid", "mcp-and-connectors", "three-agent-types"} <= names
    assert {"find-the-docs", "build-first-agent"} <= {s.spec.name for s in skills.load_hub(base)}
