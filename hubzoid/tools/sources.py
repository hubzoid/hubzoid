"""search_sources: answer from the person's own sources, with citations.

Searches the collections the caller may read (``hubzoid.chat.retrieval``),
for this agent, and returns the best passages, each with a citation marker
``[cite:<id>]``. The marker names a stored citation (source, version and
passage); the chat shows it as a link that is re-authorised when opened, so a
marker the model makes up resolves to nothing. The caller is the signed-in
person of the run (``access.current_identity``), never a name from the model.
"""
from __future__ import annotations

import logging

from agents import function_tool

log = logging.getLogger("hubzoid")


def make(ctx) -> list:
    hub_dir = ctx.hub_dir

    def _service():
        from ..chat import file_service, retrieval
        from ..chat import store as store_mod

        engine = store_mod.for_hub(hub_dir).engine
        hub = hub_dir.name.lower()
        return retrieval.service_for(hub_dir, engine, hub, file_service.service_for(hub_dir, engine, hub))

    def _caller():
        from ..access import current_identity
        from ..auth import users
        from ..chat.engine import Actor

        ident = current_identity()
        if ident.user is None:
            return None
        u = users.find_by_email(hub_dir, ident.user)
        if u is None:
            return None
        return Actor(id=u["id"], email=u["email"].lower(), groups=frozenset(ident.groups))

    def _conversation() -> str | None:
        from .. import _request_ctx

        chat = _request_ctx.get_chat_id() or ""
        return chat[4:] if chat.startswith("web-") else None

    @function_tool
    def search_sources(query: str, top_k: int = 6) -> str:
        """Search the person's own knowledge sources (the collections they can
        read) and return the most relevant passages.

        Use the facts in these passages to answer, and put each passage's
        marker, exactly as given (for example [cite:3f2a...]), right after the
        sentence that uses it. Never invent a marker. If nothing relevant is
        found, say that the sources don't cover it.

        Args:
            query: What to look for, in plain words.
            top_k: How many passages to return (1 to 10).
        """
        actor = _caller()
        if actor is None:
            return "[search_sources: sources are only available to a signed-in person.]"
        try:
            out = _service().query(actor, query, top_k=max(1, min(int(top_k or 6), 10)),
                                   agent=getattr(ctx, "agent_name", None), conversation_id=_conversation())
        except Exception as exc:  # noqa: BLE001 - the model gets a readable reason
            return f"[search_sources: {getattr(exc, 'message', type(exc).__name__)}]"
        if out["no_matches"]:
            return "No passages in your sources match that." if out["collections"] else \
                "You have no ready sources yet. Add some under Sources, then sync."
        lines = []
        for r in out["results"]:
            where = f", page {r['locator']['page']}" if r["locator"].get("page") else ""
            lines.append(f"[cite:{r['citation_id']}] {r['title']}{where}\n{r['excerpt']}")
        return "\n\n".join(lines)

    return [search_sources]
