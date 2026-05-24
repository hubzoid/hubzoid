"""Tool-result cap with overflow spillover.

When a tool result exceeds `cap` characters, write the full payload to a
file under the session output dir and return a truncated head plus a
short recovery hint that tells the model exactly where the rest is and
how to fetch more.
"""
from __future__ import annotations

import uuid
from pathlib import Path


def truncate_with_overflow(
    text: str,
    *,
    cap: int,
    overflow_dir: Path,
    label: str,
    hub_dir: Path | None = None,
) -> tuple[str, Path | None]:
    """Return (body_to_show_model, overflow_path_or_None).

    If `text` is within `cap`, the body is returned unchanged and overflow
    is None. Otherwise the full text is written to a uniquely-named file
    under `overflow_dir`, and the body is the head plus a recovery hint
    that names the overflow path (relative to `hub_dir` if provided).
    """
    if len(text) <= cap:
        return text, None

    overflow_dir.mkdir(parents=True, exist_ok=True)
    spill = overflow_dir / f"{label}-overflow-{uuid.uuid4().hex[:8]}.txt"
    spill.write_text(text, encoding="utf-8")

    shown_path: Path | str = spill
    if hub_dir is not None:
        try:
            shown_path = spill.relative_to(hub_dir)
        except ValueError:
            pass

    head = text[:cap]
    hint = (
        f"\n\n[Result truncated at {cap:,} chars "
        f"({len(text):,} total). Full output saved to `{shown_path}` — "
        f"read with read_file(path, offset, limit) to continue.]"
    )
    return head + hint, spill
