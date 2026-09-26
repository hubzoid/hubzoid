"""Core-shipped curator tool: a gated `remember` that persists learnings.

This ships with hubzoid, so every hub has it — but it is GATED. The tool
carries the ``curator`` permission, shown in Console as Save shared knowledge.
On a managed hub, the caller needs that grant on an allowed surface (including
the trusted workflow surface). Unmigrated hubs retain legacy group membership.
Anonymous callers and disallowed surfaces are refused by the same guard as
`restricted/` tools; installing the tool does not grant permission to use it.

It is user-invoked, not autonomous: the tool description instructs the model to
call ``remember`` ONLY when the user explicitly asks to remember/save/note
something, never on its own initiative. "Self-learning" here means a human
curator teaching the hub in-chat. A workflow needs an explicit curator grant and
instructions to save knowledge; the tool does not autonomously learn from chats.

What it does. ``remember(topic, content)`` writes a single knowledge document
to ``<hub>/knowledge/_learned/<slug>.md`` with frontmatter ``name:
learned/<slug>`` — creating it if new, or FULL-REPLACING it if it exists. The
LLM composes ``content`` as the complete current knowledge on that topic
(reading the prior doc first with ``read_knowledge`` when correcting), so a
correction updates in place rather than appending a contradiction. The
``learned/`` name prefix keeps a learned doc from colliding with an
operator-authored ``knowledge/<topic>.md`` under the default naming (operator
docs default to their filename stem; the loader keys by frontmatter ``name``).

Because the read tools (`read_knowledge`/`list_knowledge`) are disk-live, a
remembered fact is visible to the agent on the very next call — no restart.
Provenance (who wrote it, when) is recorded in frontmatter, not the body, so
a full-replace never accumulates stale provenance lines.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from agents import function_tool

from .._fs import resolve_bucket
from ..capabilities import Capability, register

log = logging.getLogger("hubzoid")

# The capability a caller needs to use `remember`, registered here where it is
# enforced so the Console row and the guard share one id. Factories and tests
# read CURATOR_PERMISSION; on legacy hubs it is also the OWUI group name.
CURATOR = register(Capability(
    permission="curator", label="Save shared knowledge", group="tools",
    description="Use remember to create or replace learned knowledge shared by this agent.",
    surfaces=("chat", "workflow"),
))
CURATOR_PERMISSION = CURATOR.permission

_LEARNED_SUBDIR = "_learned"
_MAX_TOPIC_LEN = 80
_MAX_CONTENT_BYTES = 100_000


def _slugify(topic: str) -> str:
    """A safe filename stem for a learned topic. Lowercase, hyphen-joined,
    alphanumeric-plus-hyphen only — so it can never traverse paths or collide
    with a directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", (topic or "").strip().lower()).strip("-")
    # rstrip again after truncation so an 80-char cut can't leave a trailing "-".
    return slug[:_MAX_TOPIC_LEN].rstrip("-")


def _learned_dir(hub_dir: Path) -> Path:
    """The `knowledge/_learned/` directory, honoring a case/plural-variant
    `knowledge/` bucket if one already exists on disk."""
    kdir = resolve_bucket(hub_dir, "knowledge") or (hub_dir / "knowledge")
    return kdir / _LEARNED_SUBDIR


def make(ctx) -> list:
    hub_dir: Path = ctx.hub_dir

    @function_tool
    def remember(topic: str, content: str) -> str:
        """Save a durable learning into the hub's knowledge base — ONLY when the
        user explicitly asks you to.

        Call this tool ONLY when the user directly tells you to remember, save,
        note, or record something for next time (e.g. "remember that...", "save
        this", "note for future", "don't forget..."). Do NOT call it on your own
        initiative: never decide to persist a fact just because it seems useful,
        and never curate silently in the background. If the user has not asked
        you to remember something, do not use this tool.

        When the user does ask, it writes ONE knowledge document per topic. If a
        document for this topic already exists, this REPLACES it in full, so
        first call `read_knowledge('learned/<topic>')` and fold your update into
        the complete text — do not send only the new sentence, send the whole
        updated document. This makes corrections (e.g. "X and Y are only
        PARTIALLY connected") overwrite the old claim instead of contradicting
        it.

        The saved learning is available to every user of this hub and is
        readable immediately via read_knowledge.

        Args:
            topic: Short subject, e.g. "billing edge cases". Becomes the
                document name `learned/<slug>`.
            content: The COMPLETE current knowledge on this topic, as markdown.
                Not an incremental note — the full document body.

        Returns:
            Confirmation with the document name, or a refusal explaining why.
        """
        slug = _slugify(topic)
        if not slug:
            return ("[remember refused: `topic` must contain letters or digits "
                    "(it becomes the document name).]")
        body = content if isinstance(content, str) else str(content)
        if not body.strip():
            return "[remember refused: `content` is empty — send the full document text.]"
        if len(body.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return (f"[remember refused: content exceeds {_MAX_CONTENT_BYTES} bytes. "
                    "Split into more focused topics — keep one topic per document.]")

        name = f"learned/{slug}"
        ldir = _learned_dir(hub_dir)
        target = (ldir / f"{slug}.md").resolve()
        # Defence in depth: the slug is already path-safe, but never leave the dir.
        if ldir.resolve() != target.parent:
            return "[remember refused: resolved path escapes the learned directory.]"

        from ..access import current_identity
        who = current_identity().user or "unknown"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existed = target.is_file()
        desc = f"Learned knowledge about {topic.strip()}".replace("\n", " ")[:200]
        # Build frontmatter with yaml.safe_dump, NOT f-string concatenation:
        # an everyday topic like "billing: edge cases" would otherwise emit
        # `description: Learned knowledge about billing: edge cases` — invalid
        # YAML that makes the loader raise and (via the disk-live fallback)
        # blanks the whole knowledge scan. safe_dump quotes/escapes every scalar.
        front = yaml.safe_dump(
            {"name": name, "description": desc, "learned_by": who, "updated": now},
            sort_keys=False, allow_unicode=True, default_flow_style=False,
        )
        doc = f"---\n{front}---\n\n{body.strip()}\n"
        try:
            ldir.mkdir(parents=True, exist_ok=True)
            # Keep one recoverable prior version (a bad full-replace is otherwise
            # unrecoverable). `.md.bak` is not picked up by the `*.md` loader.
            if existed:
                try:
                    shutil.copy2(target, target.with_suffix(".md.bak"))
                except OSError:
                    pass  # best-effort backup; never block the write
            # Atomic replace so a concurrent read never sees a torn file. Use a
            # unique temp name (not a fixed <slug>.md.tmp) so two curators
            # writing the same topic can't have one's os.replace consume the
            # other's tmp and misreport a spurious failure.
            fd, tmp_path = tempfile.mkstemp(prefix=f".{slug}.", suffix=".md.tmp", dir=str(ldir))
            tmp = Path(tmp_path)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(doc)
                os.replace(tmp, target)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("remember: write failed for %s: %s", target, exc)
            return f"[remember failed: could not write the document ({exc}).]"

        verb = "Updated" if existed else "Saved"
        log.info("curator: %s learned/%s by %s (%d bytes)", verb.lower(), slug, who,
                 len(body))
        return (f"{verb} **{name}** ({len(body)} chars). It is now part of this "
                f"hub's knowledge and readable via read_knowledge('{name}').")

    return [remember]
