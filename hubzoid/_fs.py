"""Case- and plural-insensitive folder resolution within a hub directory.

Hub authors may type `Skills/`, `skills/`, `skill/`, `Skill/` — all should
resolve to the same logical bucket. We pick the first match found on disk
(alphabetical by actual name) and warn if multiple are present.

The canonical names used internally are: agents, skills, knowledge, tools_local,
connectors, output. The mapping accepts the singular and any case variant.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Map a canonical bucket → set of acceptable directory names (lowercase).
# Singular and plural both accepted. Case-insensitive at match time.
_ALIASES: dict[str, tuple[str, ...]] = {
    "agents": ("agents", "agent"),
    "skills": ("skills", "skill"),
    "knowledge": ("knowledge",),
    "tools_local": ("tools_local", "tool_local", "tools", "local_tools"),
    "connectors": ("connectors", "connector"),
    "output": ("output", "outputs"),
    "raw_data": ("raw_data", "raw-data", "rawdata"),
    "schedule": ("schedule", "schedules", "scheduled"),
}


def resolve_bucket(hub_dir: Path, bucket: str) -> Path | None:
    """Return the actual on-disk path for the given canonical bucket, or None.

    `bucket` must be one of the keys in `_ALIASES`. Folder names are matched
    case-insensitively; singular/plural variants are accepted. If two valid
    variants exist (e.g. both `Skills/` and `skill/`), the alphabetically-first
    match is returned and a warning is logged.
    """
    if bucket not in _ALIASES:
        raise ValueError(f"unknown bucket: {bucket!r}")
    accepted = {a.lower() for a in _ALIASES[bucket]}

    matches: list[Path] = []
    if hub_dir.is_dir():
        for child in sorted(hub_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and child.name.lower() in accepted:
                matches.append(child)

    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        log.warning(
            "multiple folders match the %r bucket (%s) — using %r. "
            "Consolidate to one to avoid surprises.",
            bucket, names, matches[0].name,
        )
    return matches[0]
