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

Public entry: `rewrite_owui_prompt(prompt, owui_uploads_dir) -> str | None`.

When the prompt is a recognisable OWUI RAG wrap with at least one file
that exists on disk, returns the rewritten prompt:

    [User attached file: foo.json — read with read_file('/abs/path/...')
     or pass this path to test_template, extract_for_review,
     validate_template, or any path-accepting tool.]

    {user query verbatim}

When the prompt doesn't match (plain question, no <context>, no <source>,
all files missing), returns None and the caller passes the original
prompt through unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

# Match a single <source ...> tag. We extract attributes individually
# (rather than positionally) because OWUI's attribute order may vary.
_SOURCE_TAG_RE = re.compile(r"<source\s+([^>]+)>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def parse_owui_attachment_prompt(
    prompt: str,
    owui_uploads_dir: Path,
) -> tuple[list[tuple[str, Path]], str] | None:
    """Extract (file paths, user query) from an OWUI RAG-wrapped prompt.

    Returns None when the prompt isn't a recognisable OWUI wrap, when
    no `<source resource-type="file">` tags exist, or when none of the
    referenced files are on disk. On success, returns:

        ([(filename, absolute_path), ...], user_query)

    `user_query` is everything after the closing `</context>` tag,
    stripped of surrounding whitespace. Filename + path comes straight
    from the `name` and `resource-id` attributes of each `<source>`.
    Duplicate (file_id, name) pairs are coalesced — OWUI emits one
    `<source>` per retrieved chunk and we only want each file once.
    """
    if "</context>" not in prompt:
        return None

    refs: list[tuple[str, str]] = []  # ordered (file_id, name) for stable output
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

    if not refs:
        return None

    paths: list[tuple[str, Path]] = []
    for file_id, name in refs:
        target = owui_uploads_dir / f"{file_id}_{name}"
        if target.is_file():
            paths.append((name, target))

    if not paths:
        return None

    user_query = prompt.rsplit("</context>", 1)[-1].strip()
    return paths, user_query


def rewrite_owui_prompt(prompt: str, owui_uploads_dir: Path) -> str | None:
    """Rewrite an OWUI RAG-wrapped prompt into clean notes + user query.

    Returns None when the prompt isn't an OWUI wrap (caller should pass
    the original prompt through). Returns a string when we have
    attachments + a user query to surface.

    The rewritten prompt:

        [User attached file: NAME — read with read_file('PATH') or pass
         this path to test_template, extract_for_review, validate_template,
         or any path-accepting tool.]
        [User attached file: ...]

        USER_QUERY

    The OWUI wrapper boilerplate and the `<context>...</context>` block
    with RAG chunks are dropped entirely — the agent reads the full file
    from disk via `read_file`, so the chunks are redundant token cost.
    """
    parsed = parse_owui_attachment_prompt(prompt, owui_uploads_dir)
    if parsed is None:
        return None
    paths, user_query = parsed
    notes = "\n".join(
        f"[User attached file: {name} — read with read_file('{path}') "
        f"or pass this path to test_template, extract_for_review, "
        f"validate_template, or any path-accepting tool.]"
        for name, path in paths
    )
    if user_query:
        return f"{notes}\n\n{user_query}"
    return notes
