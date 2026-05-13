"""File operations scoped to the hub directory.

Reads: anywhere under the hub directory (read-only by design).
Writes: only under <hub>/output/<session_id>/ — the agent cannot scribble over
        its own source files.

`session_id` is taken from the hub context. v1 uses a single session per
process boot; multi-session is a v1.1 concern.
"""
from __future__ import annotations

from pathlib import Path

from agents import function_tool


def make(ctx) -> list:
    hub_dir: Path = ctx.hub_dir
    output_root: Path = ctx.output_dir

    @function_tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file under the hub directory.

        Args:
            path: Path relative to the hub root, or absolute (must resolve inside the hub).

        Returns:
            File contents, truncated to 200_000 characters if larger.
        """
        target = (hub_dir / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if hub_dir.resolve() not in target.parents and target != hub_dir.resolve():
            return f"[read_file refused: {path!r} is outside the hub directory]"
        if not target.is_file():
            return f"[read_file: {path!r} not found]"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > 200_000:
            text = text[:200_000] + "\n\n[truncated]"
        return text

    @function_tool
    def list_files(glob: str = "**/*") -> str:
        """List files under the hub directory matching a glob (default: all).

        Args:
            glob: Glob pattern relative to the hub root, e.g. "knowledge/*.md".

        Returns:
            Newline-separated list of relative paths. Empty string if nothing matches.
        """
        matches = sorted(p for p in hub_dir.glob(glob) if p.is_file())
        if not matches:
            return ""
        return "\n".join(str(p.relative_to(hub_dir)) for p in matches[:500])

    @function_tool
    def write_artifact(filename: str, content: str) -> str:
        """Write a file under <hub>/output/<session>/.

        Args:
            filename: File name (no directory traversal). Sub-paths under output/ are allowed.
            content: UTF-8 text to write.

        Returns:
            The absolute path written, prefixed by "wrote: ".
        """
        target = (output_root / filename).resolve()
        if output_root.resolve() not in target.parents and target != output_root.resolve():
            return f"[write_artifact refused: {filename!r} escapes the output directory]"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote: {target}"

    return [read_file, list_files, write_artifact]
