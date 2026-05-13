"""Skill tools: list available skills, load a skill body on demand.

This is the progressive-disclosure pattern from the AGENTS.md / SKILL.md
ecosystem. The agent sees a short menu of skills and only loads the body
when relevant.
"""
from __future__ import annotations

from agents import function_tool


def make(ctx) -> list:
    skills = {s.spec.name: s for s in ctx.skills}

    def _menu() -> str:
        if not skills:
            return "(no skills in this hub)"
        rows = [f"- {s.spec.name}: {s.spec.description}" for s in ctx.skills]
        return "\n".join(rows)

    @function_tool
    def list_skills() -> str:
        """List every skill (playbook) available in this hub.

        Returns:
            Markdown bullet list of `name: description`.
        """
        return _menu()

    @function_tool
    def load_skill(name: str) -> str:
        """Load the full body of a skill by name.

        Args:
            name: The `name` from the skill's SKILL.md frontmatter.

        Returns:
            The markdown body of the skill, or an error if not found.
        """
        s = skills.get(name)
        if s is None:
            return (
                f"[load_skill: no skill named {name!r}. "
                f"Available:\n{_menu()}]"
            )
        return s.body

    return [list_skills, load_skill]
