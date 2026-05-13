"""Knowledge tools: list and read documents under <hub>/knowledge/.

Knowledge files are loaded once at agent build; these tools surface them
to the agent at runtime.
"""
from __future__ import annotations

from agents import function_tool


def make(ctx) -> list:
    knowledge = {k.name: k for k in ctx.knowledge}

    def _menu() -> str:
        if not knowledge:
            return "(no knowledge files in this hub)"
        rows = [f"- {k.name}: {k.description}" for k in ctx.knowledge]
        return "\n".join(rows)

    @function_tool
    def list_knowledge() -> str:
        """List every knowledge document available in the hub.

        Returns:
            Markdown bullet list of `name: description`.
        """
        return _menu()

    @function_tool
    def read_knowledge(name: str) -> str:
        """Read the full body of a knowledge document by name.

        Args:
            name: The `name` from the document's frontmatter (or its filename stem).

        Returns:
            The markdown body of the requested document, or an error if not found.
        """
        doc = knowledge.get(name)
        if doc is None:
            return (
                f"[read_knowledge: no document named {name!r}. "
                f"Available:\n{_menu()}]"
            )
        return doc.body

    return [list_knowledge, read_knowledge]
