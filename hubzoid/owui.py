"""Open WebUI RAG-template attachment parser.

OWUI wraps the user's question in a RAG template before forwarding to
external OpenAI-compatible endpoints. The wrapped prompt contains
`<source>` tags for retrieved file chunks; each tag carries the
`resource-id` (OWUI's file_id) and `name` (filename) directly. OWUI
persists every uploaded file to a deterministic path:

    <hub>/.openwebui-data/uploads/<file_id>_<filename>

So we don't need to query OWUI's SQLite DB, we don't need correlation
headers, and we don't need to match user-query text to a chat row.
The file_id + filename are in the prompt itself.

This module ONLY parses — it does not decide where the bytes live. The
bridge (`server._normalize_owui_uploads`) is the one place that reads
each parsed file and copies it into the canonical per-chat uploads store,
so that read_upload / vision / ticket attachments all resolve OWUI files
from the SAME directory as Slack/base64 uploads. Nothing downstream of
the bridge needs to know Open WebUI exists.

Public entry: `owui_attachments(prompt, owui_uploads_dir)` -> (resolved,
unresolved, user_query) or None. `parse_owui_attachment_prompt` is a
thin resolved-only wrapper kept for callers that only want files that
exist on disk.
"""
from __future__ import annotations

import re
from pathlib import Path

# Match a single <source ...> tag. We extract attributes individually
# (rather than positionally) because OWUI's attribute order may vary.
_SOURCE_TAG_RE = re.compile(r"<source\s+([^>]+)>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def _file_refs(prompt: str) -> list[tuple[str, str]]:
    """Deduped, ordered (file_id, name) for every `<source resource-type="file">`
    tag in the prompt. OWUI emits one `<source>` per retrieved chunk, so the same
    file appears many times — we want each once, in first-seen order."""
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in _SOURCE_TAG_RE.finditer(prompt):
        attrs = dict(_ATTR_RE.findall(tag.group(1)))
        if attrs.get("resource-type") != "file":
            continue
        file_id = attrs.get("resource-id")
        name = attrs.get("name")
        if not file_id or not name:
            continue
        key = (file_id, name)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def owui_attachments(
    prompt: str,
    owui_uploads_dir: Path,
) -> tuple[list[tuple[str, Path]], list[str], str] | None:
    """Parse an OWUI RAG-wrapped prompt into its file attachments.

    Returns None when `prompt` is not an OWUI wrap at all — no `</context>`, or
    a `<context>` that carries no file `<source>` tags (e.g. a knowledge
    collection). The caller passes such a prompt through untouched.

    Otherwise returns `(resolved, unresolved, user_query)`:

        resolved   : [(name, absolute_path), ...] file refs whose bytes are on disk
        unresolved : [name, ...]                   file refs whose bytes are missing
        user_query : text after `</context>`, stripped

    `unresolved` is surfaced deliberately: a file OWUI referenced but whose bytes
    we cannot find (cleaned up, or the template drifted) must become a LOUD note,
    never a silent drop — a silent drop is what makes a user paste a whole file
    into chat.
    """
    if "</context>" not in prompt:
        return None
    refs = _file_refs(prompt)
    if not refs:
        return None

    resolved: list[tuple[str, Path]] = []
    unresolved: list[str] = []
    for file_id, name in refs:
        target = owui_uploads_dir / f"{file_id}_{name}"
        if target.is_file():
            resolved.append((name, target))
        else:
            unresolved.append(name)

    user_query = prompt.rsplit("</context>", 1)[-1].strip()
    return resolved, unresolved, user_query


def parse_owui_attachment_prompt(
    prompt: str,
    owui_uploads_dir: Path,
) -> tuple[list[tuple[str, Path]], str] | None:
    """Resolved-only view of `owui_attachments`: `([(name, path), ...], user_query)`,
    or None when the prompt is not an OWUI wrap or none of its referenced files
    exist on disk. Kept for callers that only care about readable files."""
    result = owui_attachments(prompt, owui_uploads_dir)
    if result is None:
        return None
    resolved, _unresolved, user_query = result
    if not resolved:
        return None
    return resolved, user_query
