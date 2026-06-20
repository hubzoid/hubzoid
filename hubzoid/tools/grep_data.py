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

import re
import shutil
import subprocess
from pathlib import Path

from agents import function_tool

from .. import _fs
from .._fs import resolve_bucket
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

        target = _resolve_inside_hub(hub_dir, path)
        if target is None:
            return f"[grep_data refused: {path!r} is outside the hub directory]"
        if _fs.is_under_restricted(hub_dir, target):
            return f"[grep_data refused: {path!r} is in the restricted/ folder]"
        if not target.exists():
            return f"[grep_data: {path!r} not found]"

        context = max(0, min(5, int(context)))

        if shutil.which("rg"):
            hits = _run_rg(pattern, target, context)
        else:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                return f"[grep_data: invalid regex {pattern!r} ({exc})]"
            hits = _run_python(regex, target, context)

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


# --- Path resolution -------------------------------------------------------
def _resolve_inside_hub(hub_dir: Path, path: str) -> Path | None:
    target = (hub_dir / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    hub_root = hub_dir.resolve()
    if target == hub_root or hub_root in target.parents:
        return target
    return None


# --- Backends --------------------------------------------------------------
def _run_rg(pattern: str, target: Path, context: int) -> list[tuple[str, int, str]]:
    """Shell out to ripgrep. Returns [(rel_path_from_target, line_no, line_text)]."""
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]
    for d in IGNORE_DIRS:
        cmd.extend(["--glob", f"!{d}"])
    if context:
        cmd.extend(["-C", str(context)])
    cmd.extend(["--", pattern, str(target)])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        return []

    out: list[tuple[str, int, str]] = []
    for line in proc.stdout.splitlines():
        parsed = _parse_rg_line(line, target)
        if parsed:
            out.append(parsed)
    return out


def _parse_rg_line(line: str, target: Path) -> tuple[str, int, str] | None:
    """rg lines: '<path>:<lineno>:<content>' (or '-' for context separator)."""
    if not line or line == "--":
        return None
    parts = line.split(":", 2)
    if len(parts) < 3:
        return None
    raw_path, lineno_str, content = parts
    try:
        lineno = int(lineno_str)
    except ValueError:
        return None
    return (raw_path, lineno, content)


def _run_python(regex: re.Pattern, target: Path, context: int) -> list[tuple[str, int, str]]:
    """Walk target with os.walk; skip ignored dirs, binaries, oversized files."""
    out: list[tuple[str, int, str]] = []
    if target.is_file():
        out.extend(_grep_file(regex, target, context))
        return out

    for p in _walk(target):
        out.extend(_grep_file(regex, p, context))
        if len(out) > MAX_MATCHES * 2:  # short-circuit; cap-format trims later
            break
    return out


def _walk(root: Path):
    """Yield files under root, skipping ignored dirs and obvious binaries."""
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


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
