"""Knowledge tools: list and read documents under <hub>/knowledge/.

These tools re-scan `<hub>/knowledge/` from disk on EVERY call rather than a
snapshot taken at agent-build time. That is what lets a durable learning
written by the curator tool (`remember`) — or any live edit to a knowledge
file — be visible to the agent immediately, without a bridge restart. The
build-time snapshot on `ctx.knowledge` is still used for the static knowledge
menu in the system prompt (see `system_addendum`), which only refreshes when
the runtime is rebuilt; these runtime tools are the always-fresh path.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .._fs import resolve_bucket
from ..loaders import knowledge as knowledge_loader

log = logging.getLogger("hubzoid")


def make(ctx) -> list:
    hub_dir = ctx.hub_dir
    # (sig, docs) kept in ONE slot so a concurrent reader never sees a new sig
    # paired with old docs. The SDK dispatches sync tool bodies via
    # asyncio.to_thread, so two knowledge reads can race in parallel OS threads;
    # a single dict-subscript read/write of the pair is atomic under the GIL.
    cache: dict = {"state": (object(), None)}   # sentinel sig != any real one

    def _signature():
        """Cheap fingerprint of the knowledge tree (paths + mtime + size), via
        stat only — no YAML parse. Reflects adds, removes, and edits, so the
        parsed-doc cache stays disk-live while skipping re-parses within a turn."""
        kdir = resolve_bucket(hub_dir, "knowledge")
        if kdir is None:
            return ()
        sig = []
        for p in kdir.rglob("*.md"):
            try:
                st = p.stat()
            except OSError:
                continue
            sig.append((p.as_posix(), st.st_mtime_ns, st.st_size))
        sig.sort()
        return tuple(sig)

    def _docs() -> list:
        """Current knowledge docs, read live from disk with an mtime cache.
        Falls back to the build-time snapshot if a disk scan fails."""
        try:
            sig = _signature()
            csig, cdocs = cache["state"]       # one atomic read of the pair
            if sig == csig and cdocs is not None:
                return cdocs
            docs = knowledge_loader.load_all(hub_dir)
            cache["state"] = (sig, docs)       # one atomic write of the pair
            return docs
        except Exception as exc:  # noqa: BLE001 — never break the tool on a bad file
            log.warning("read_knowledge: live scan failed (%s); using snapshot", exc)
            return list(ctx.knowledge)

    def _menu() -> str:
        docs = _docs()
        if not docs:
            return "(no knowledge files in this hub)"
        return "\n".join(f"- {k.name}: {k.description}" for k in docs)

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
        docs = {k.name: k for k in _docs()}
        doc = docs.get(name)
        if doc is None:
            return (
                f"[read_knowledge: no document named {name!r}. "
                f"Available:\n{_menu()}]"
            )
        return doc.body

    return [list_knowledge, read_knowledge]
