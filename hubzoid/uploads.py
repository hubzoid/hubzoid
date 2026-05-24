"""Upload sidecar metadata.

Every file the bridge writes into a chat's `uploads/` directory gets a
matching `{filename}.hubzoid.json` sidecar holding the original mime,
size, and a coarse kind classification. The kind is used by
`read_upload` to pick a type-aware preview (text head, JSON summary, CSV
header + row count, PDF page extract, image metadata, binary hex
preview) without having to re-sniff the bytes on every call.

Kinds: text | json | csv | pdf | image | binary
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

SIDECAR_SUFFIX = ".hubzoid.json"


def classify(mime: str, payload: bytes) -> str:
    """Coarse content kind from mime + a peek at the bytes."""
    m = (mime or "").lower().split(";")[0].strip()
    if m.startswith("image/"):
        return "image"
    if m == "application/pdf":
        return "pdf"
    if m == "application/json" or m.endswith("+json"):
        return "json"
    if m in ("text/csv", "text/tab-separated-values"):
        return "csv"
    if m.startswith("text/"):
        return "text"
    # Generic mime: fall back to a peek. UTF-8 decodability over the
    # first 4 KiB is a reasonable proxy for "the agent can read it as text".
    head = payload[:4096]
    if not head:
        return "text"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    # Filter out files that are mostly NULs / control chars even if they
    # technically decode (e.g. some UTF-16-without-BOM blobs).
    nontext = sum(1 for b in head if b < 9 or (13 < b < 32) and b != 27)
    if nontext > len(head) * 0.1:
        return "binary"
    return "text"


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Mime sniff from a bare filename, with a generic fallback."""
    m, _ = mimetypes.guess_type(filename)
    return m or fallback


def write_with_meta(
    upload_dir: Path,
    filename: str,
    payload: bytes,
    *,
    mime: str,
) -> None:
    """Write the payload and a sidecar in one shot.

    The sidecar is named `{filename}.hubzoid.json`. `read_upload` filters
    sidecars out of directory listings so the agent never sees them.
    """
    target = upload_dir / filename
    target.write_bytes(payload)
    meta = {"mime": mime, "size": len(payload), "kind": classify(mime, payload)}
    (upload_dir / f"{filename}{SIDECAR_SUFFIX}").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def read_meta(upload_dir: Path, filename: str) -> dict | None:
    """Return the sidecar dict for `filename` or None if missing/malformed.

    `read_upload` falls back to on-the-fly classification when no sidecar
    is found — preserves behaviour for files written before this module
    existed.
    """
    path = upload_dir / f"{filename}{SIDECAR_SUFFIX}"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_sidecar(filename: str) -> bool:
    return filename.endswith(SIDECAR_SUFFIX)
