"""grep_data: search inside <hub>/raw_data/ for a pattern.

Wraps system `rg` (ripgrep) when available; falls back to a pure-Python
regex walk otherwise. Both backends return the same `path:line:content`
format so the agent does not have to care which one ran.

Caps are critical — without them a single grep against a multi-repo dump
can blow the context window. We enforce three:

  * MAX_MATCHES total across all files
  * MAX_PER_FILE matches per file
  * RESULT_CAP characters in the final string (overflow spills to disk)

Each cap returns a refine hint so the model knows what to narrow next.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from agents import function_tool

from .._fs import resolve_bucket
from .files import _read_refusal
from ._caps import truncate_with_overflow

# --- Caps ------------------------------------------------------------------
MAX_MATCHES = 100
MAX_PER_FILE = 30
RESULT_CAP = 25_000

# Files larger than this are skipped by the Python backend (rg has its own
# heuristics). Defensive: keeps a single huge .sql dump from wedging the loop.
MAX_FILE_BYTES = 5 * 1024 * 1024

# Directories the agent almost never wants to grep.
IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target",
})


def make(ctx) -> list:
    hub_dir: Path = ctx.hub_dir
    output_dir: Path = ctx.output_dir

    @function_tool
    def grep_data(pattern: str, path: str = "raw_data", context: int = 0) -> str:
        """Search inside <hub>/raw_data/ for a regex pattern.

        Args:
            pattern: Regex (or plain string) to search for. Use simple
                literal strings unless you need regex syntax.
            path: Subpath under the hub root. Defaults to "raw_data".
                Scope as narrowly as possible — e.g. "raw_data/repo-a/src"
                — to avoid pulling matches from unrelated repos.
            context: Lines of surrounding context per match (0–5).
                Default 0. Each extra context line multiplies result size.

        Returns:
            Newline-separated `path:line:content` matches, grouped by file
            (most-matches first). If a cap is hit, the footer tells you
            how to narrow.
        """
        rd = resolve_bucket(hub_dir, "raw_data")
        if rd is None:
            return "[grep_data: raw_data/ is not present in this hub.]"

        target = hub_dir / path
        reason = _read_refusal(hub_dir, target)
        if reason:
            return f"[grep_data refused: {path!r}: {reason}]"
        if not target.exists():
            return f"[grep_data: {path!r} not found]"

        context = max(0, min(5, int(context)))

        if shutil.which("rg"):
            hits = _run_rg(pattern, target, context, hub_dir)
        else:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"[grep_data: invalid regex {pattern!r} ({exc})]"
            hits = _run_python(regex, target, context, hub_dir)

        body = _format(hits, hub_dir)
        body, _ = truncate_with_overflow(
            body,
            cap=RESULT_CAP,
            overflow_dir=output_dir,
            label="grep",
            hub_dir=hub_dir,
        )
        return body

    return [grep_data]


# --- Backends --------------------------------------------------------------
def _run_rg(pattern: str, target: Path, context: int, hub_dir: Path) -> list[tuple[str, int, str]]:
    """Search batches of approved files, never give rg a directory to recurse.

    --no-config prevents local ripgrep configuration from adding a preprocessor
    or changing traversal. JSON preserves filenames and surrounding context.
    """
    from itertools import islice

    paths = iter(_walk(target, hub_dir))
    out: list[tuple[str, int, str]] = []
    while batch := list(islice(paths, 64)):
        cmd = ["rg", "--no-config", "--json", "--max-count", str(MAX_PER_FILE + 1)]
        if context:
            cmd.extend(["-C", str(context)])
        cmd.extend(["--", pattern, *(str(p) for p in batch)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            break
        allowed = {str(p) for p in batch}
        for line in proc.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") not in ("match", "context"):
                continue
            data = event["data"]
            path = data["path"].get("text")
            content = data["lines"].get("text")
            if path in allowed and content is not None:
                out.append((path, data["line_number"], content.rstrip("\r\n")))
        if len(out) > MAX_MATCHES * 2:
            break
    return out


def _run_python(regex: re.Pattern, target: Path, context: int, hub_dir: Path) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for p in _walk(target, hub_dir):
        out.extend(_grep_file(regex, p, context))
        if len(out) > MAX_MATCHES * 2:
            break
    return out


def _walk(root: Path, hub_dir: Path):
    """The same per-file authorization and traversal for BOTH search backends."""
    import os

    def allowed_file(p: Path) -> bool:
        if _read_refusal(hub_dir, p):
            return False
        try:
            return p.is_file() and p.stat().st_size <= MAX_FILE_BYTES
        except OSError:
            return False

    if root.is_file():
        if allowed_file(root):
            yield root.absolute()
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and not _read_refusal(hub_dir, Path(dirpath) / d)]
        for name in filenames:
            p = Path(dirpath) / name
            if allowed_file(p):
                yield p.absolute()


def _grep_file(regex: re.Pattern, path: Path, context: int) -> list[tuple[str, int, str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw[:8192]:  # crude binary check
        return []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    hits: list[tuple[str, int, str]] = []
    for i, line in enumerate(lines, start=1):
        if regex.search(line):
            if context == 0:
                hits.append((str(path), i, line))
            else:
                lo = max(1, i - context)
                hi = min(len(lines), i + context)
                for j in range(lo, hi + 1):
                    hits.append((str(path), j, lines[j - 1]))
    return hits


# --- Formatting + caps -----------------------------------------------------
def _format(hits: list[tuple[str, int, str]], hub_dir: Path) -> str:
    if not hits:
        return "[grep_data: no matches]"

    # Group by file, count for sorting.
    by_file: dict[str, list[tuple[int, str]]] = {}
    for raw_path, lineno, line in hits:
        try:
            rel = str(Path(raw_path).resolve().relative_to(hub_dir.resolve()))
        except ValueError:
            rel = raw_path
        by_file.setdefault(rel, []).append((lineno, line))

    files_sorted = sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    lines_out: list[str] = []
    shown_matches = 0
    files_with_more: list[tuple[str, int]] = []  # (file, hidden_count)
    files_shown = 0

    for rel, entries in files_sorted:
        if shown_matches >= MAX_MATCHES:
            break
        files_shown += 1
        per_file_cap = min(MAX_PER_FILE, MAX_MATCHES - shown_matches)
        kept = entries[:per_file_cap]
        if len(entries) > per_file_cap:
            files_with_more.append((rel, len(entries) - per_file_cap))
        for lineno, line in kept:
            # Trim very long lines so one bloated match can't dominate.
            content = line if len(line) <= 300 else line[:300] + "…"
            lines_out.append(f"{rel}:{lineno}:{content}")
        shown_matches += len(kept)

    footer_parts: list[str] = []
    hidden_files = len(files_sorted) - files_shown
    total_matches = sum(len(v) for v in by_file.values())
    if total_matches > shown_matches:
        footer_parts.append(
            f"Showing {shown_matches} of ~{total_matches} matches across "
            f"{files_shown} of {len(files_sorted)} files."
        )
    if files_with_more:
        sample = ", ".join(f"{f} (+{n})" for f, n in files_with_more[:3])
        footer_parts.append(
            f"Some files have more matches than shown (e.g. {sample}). "
            f"Read those files directly to see all."
        )
    if hidden_files > 0:
        footer_parts.append(
            f"Refine: narrow `path` (e.g. path='raw_data/<one-repo>/') "
            f"or use a more specific pattern."
        )

    if footer_parts:
        lines_out.append("")
        lines_out.append("[" + " ".join(footer_parts) + "]")

    return "\n".join(lines_out)
