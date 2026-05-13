"""YAML frontmatter parsing for markdown files.

Standard pattern:

    ---
    key: value
    list_key:
      - a
      - b
    ---

    Body text in markdown.

We parse the frontmatter into a dict, return the body as a string. Validation
is the caller's job (each loader has its own pydantic model).
"""
from __future__ import annotations

from typing import Any

import yaml

DELIM = "---"


def split(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_text).

    If the file has no frontmatter (does not start with `---`), returns
    ({}, original_text). The body has leading whitespace stripped.
    """
    if not text.startswith(DELIM):
        return {}, text.strip()

    # Locate the closing delimiter. We look for a line that is exactly `---`.
    lines = text.splitlines(keepends=True)
    if len(lines) < 2 or lines[0].rstrip("\r\n") != DELIM:
        return {}, text.strip()

    close_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == DELIM:
            close_idx = i
            break
    if close_idx is None:
        # Opening delim with no close — treat as no frontmatter.
        return {}, text.strip()

    raw_yaml = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1:]).strip()

    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a YAML mapping (key/value pairs)")
    return data, body


def read(path) -> tuple[dict[str, Any], str]:
    """Convenience: read a file and split into (frontmatter, body)."""
    return split(open(path, encoding="utf-8").read())
