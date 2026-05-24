"""Type-aware previews for `read_upload`.

Each renderer is a pure `(payload: bytes, params...) -> str`. The tool
layer in `hubzoid.tools.files` dispatches to one of these based on the
sidecar's `kind` (`hubzoid.uploads.classify`). Keeping previews here
means the tool function stays small and the previews can grow type
support without churning the tool surface.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

# Small-file short-circuit thresholds: if BOTH are satisfied we return
# the whole text content unchanged instead of head + footer. Saves the
# agent a second tool call for the common case of a tiny attachment.
SMALL_TEXT_MAX_LINES = 500
SMALL_TEXT_MAX_BYTES = 256 * 1024

DEFAULT_TEXT_LIMIT_LINES = 200
CSV_PREVIEW_ROWS = 20
DEFAULT_PDF_PAGES = 5
BINARY_HEX_BYTES = 256

# Hard upper bound for `read_upload_full`. Files that exceed this are
# truncated rather than rejected — agents asking for "full" usually
# want as much as they can get, and an explicit byte-count footer is
# a clearer signal than a refusal.
#
# 500_000 chars ≈ 125_000 tokens at ~4 chars/token. Fits comfortably in
# any modern model's context (Sonnet/Opus 4.x = 200k+, Sonnet 4.6 with
# the 1M flag = 1M). Covers most real attachments (architecture docs,
# JSON exports, transcripts). At 250k we were truncating 1MB markdown
# files, which made the agent panic and escape to Bash on hallucinated
# paths — see `_uploads_section` in system_addendum.py.
READ_FULL_MAX_CHARS = 500_000


def text_preview(payload: bytes, *, offset: int, limit: int) -> str:
    """Head/limit/offset slice of a UTF-8 text payload.

    `offset` is 1-indexed. Returns the slice plus a footer telling the
    agent how to paginate if the file is larger than what was returned.
    Small files (both line- and byte-bounded) come through whole with no
    footer to preserve the v0 contract.
    """
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    if (
        offset == 1
        and limit >= total
        and total <= SMALL_TEXT_MAX_LINES
        and len(payload) <= SMALL_TEXT_MAX_BYTES
    ):
        return text

    start = max(0, offset - 1)
    end = min(total, start + max(1, limit))
    if start >= total:
        return (
            f"[read_upload: offset {offset} past end of {total}-line file. "
            f"Use offset between 1 and {total}.]"
        )

    body = "\n".join(lines[start:end])
    shown_end = end  # already exclusive in slice; report inclusive-end below
    body_bytes = sum(len(ln) for ln in lines[start:end]) + max(0, end - start - 1)
    next_offset = shown_end + 1
    if shown_end >= total:
        # We returned to end-of-file; just state what was included.
        footer = (
            f"\n\n[End of file. Returned lines {start + 1}-{shown_end} "
            f"({body_bytes}/{len(payload)} bytes of {total} total lines).]"
        )
    else:
        footer = (
            f"\n\n[Returned lines {start + 1}-{shown_end} "
            f"({body_bytes}/{len(payload)} bytes, {total} total lines). "
            f"Call read_upload(filename, offset={next_offset}) for the next page.]"
        )
    return body + footer


def json_preview(payload: bytes) -> str:
    """Structural summary of a JSON payload plus the pretty head.

    The summary is what gets the agent unstuck on a 5 MB JSON without
    paging through it: top-level keys for an object, length + first
    element shape for an array, scalar value otherwise.
    """
    try:
        obj = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        # Fall back to text head so the agent can still see what's there.
        return f"[read_upload: invalid JSON ({exc.msg}). Showing raw head:]\n\n" + text_preview(
            payload, offset=1, limit=50
        )
    summary = _summarize_json(obj)
    pretty = json.dumps(obj, indent=2, ensure_ascii=False)
    pretty_lines = pretty.splitlines()
    head = "\n".join(pretty_lines[:50])
    if len(pretty_lines) > 50:
        head += (
            f"\n\n[Returned first 50 of {len(pretty_lines)} pretty-printed lines. "
            f"Call read_upload_full(filename) for the entire JSON document.]"
        )
    return summary + "\n\n" + head


def _summarize_json(obj) -> str:
    if isinstance(obj, dict):
        keys = list(obj.keys())
        return f"[JSON object with {len(keys)} top-level keys: {', '.join(repr(k) for k in keys[:20])}{' ...' if len(keys) > 20 else ''}]"
    if isinstance(obj, list):
        n = len(obj)
        if n == 0:
            return "[JSON array, empty]"
        first_kind = type(obj[0]).__name__
        first_keys = ""
        if isinstance(obj[0], dict):
            first_keys = f" element keys: {', '.join(repr(k) for k in list(obj[0].keys())[:20])}"
        return f"[JSON array of {n} items (first item is {first_kind}{first_keys})]"
    return f"[JSON scalar ({type(obj).__name__}): {obj!r}]"


def csv_preview(payload: bytes) -> str:
    """Header + first N data rows + total row count.

    Handles both LF and CRLF line endings. Empty lines at the end (a
    common csv-writer quirk) don't inflate the row count.
    """
    text = payload.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "[CSV: empty file]"
    header = lines[0]
    rows = lines[1:]
    preview_rows = rows[:CSV_PREVIEW_ROWS]
    body = "\n".join([header, *preview_rows])
    footer = (
        f"\n\n[CSV with {len(rows)} data rows. "
        f"Showing header + first {len(preview_rows)} rows.]"
    )
    return body + footer


def pdf_preview(payload: bytes, *, pages: str | None) -> str:
    """Extract text from a page range. Default is first DEFAULT_PDF_PAGES pages.

    `pages` is a `"a-b"` range (1-indexed, inclusive) or a single
    `"n"`. Returns one labelled chunk per extracted page so the agent
    can keep its bearings.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[read_upload: pypdf not installed; cannot extract PDF text]"
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:  # noqa: BLE001
        return f"[read_upload: failed to open PDF ({type(exc).__name__}: {exc})]"

    total = len(reader.pages)
    start, end = _parse_page_range(pages, total)
    chunks: list[str] = [f"[PDF: {total} pages. Showing pages {start}-{end}.]"]
    for i in range(start - 1, end):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            text = f"(page {i + 1} extraction failed: {exc})"
        chunks.append(f"\n--- page {i + 1} ---\n{text.strip()}")
    if end < total:
        chunks.append(
            f"\n[Use pages='{end + 1}-{min(total, end + DEFAULT_PDF_PAGES)}' "
            f"for the next chunk.]"
        )
    return "\n".join(chunks)


def _parse_page_range(spec: str | None, total: int) -> tuple[int, int]:
    """Return (start, end) 1-indexed inclusive, clamped to [1, total]."""
    if not spec:
        return 1, min(total, DEFAULT_PDF_PAGES)
    spec = spec.strip()
    if "-" in spec:
        a, _, b = spec.partition("-")
        try:
            start = max(1, int(a))
            end = min(total, int(b))
        except ValueError:
            return 1, min(total, DEFAULT_PDF_PAGES)
    else:
        try:
            start = max(1, int(spec))
            end = start
        except ValueError:
            return 1, min(total, DEFAULT_PDF_PAGES)
    if end < start:
        end = start
    return start, min(end, total)


def image_preview(filename: str, *, mime: str, size: int, target: Path) -> str:
    """Metadata-only response — we don't read image bytes into the prompt.

    The agent gets enough to decide whether to surface the path to the
    user or hand the image to a vision-capable tool downstream.
    """
    return (
        f"[Image file: {filename} ({mime}, {size} bytes).\n"
        f"Path on disk: {target}\n"
        f"read_upload does not return image bytes — pass the path to a "
        f"vision-capable tool to inspect the contents.]"
    )


def binary_preview(filename: str, *, payload: bytes, target: Path) -> str:
    """Hex preview of the first BINARY_HEX_BYTES + size note.

    Lets the agent at least see a magic number / header before deciding
    what to do, without flooding the prompt with raw bytes.
    """
    head = payload[:BINARY_HEX_BYTES]
    hex_bytes = " ".join(f"{b:02x}" for b in head)
    suffix = "" if len(payload) <= BINARY_HEX_BYTES else " ..."
    return (
        f"[Binary file: {filename} ({len(payload)} bytes).\n"
        f"Path on disk: {target}\n"
        f"First {len(head)} bytes (hex): {hex_bytes}{suffix}]"
    )
