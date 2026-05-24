"""File operations.

  * `read_file`        — read anything under the hub directory (read-only).
  * `list_files`       — glob the hub directory.
  * `write_artifact`   — write to the current chat's artifacts directory.
                         Returns a markdown download link the agent passes
                         straight to the user.
  * `read_upload`      — read a file the user uploaded to the current chat.
  * `list_artifacts`   — list files written in the current chat so far.

The two chat-scoped tools resolve their directory from the request-scoped
`chat_id` ContextVar set by the bridge. If no chat is in scope (CLI test
runs, unit tests), they fall back to the process-boot session output dir
under `<hub>/output/<session_id>/` so existing behaviour is preserved.

Filenames passed to `write_artifact` are sanitised: any directory
components are stripped (so `output/file.png` becomes `file.png`). The
agent never has to think about paths — the framework owns them.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from agents import function_tool

from .. import _request_ctx
from .. import _signing
from .. import memory as memlib
from .. import upload_previews
from .. import uploads as uploads_lib
from ._caps import truncate_with_overflow as _truncate


_READ_FILE_CAP = 25_000
_LIST_FILES_CAP = 100


def make(ctx) -> list:
    hub_dir: Path = ctx.hub_dir
    fallback_dir: Path = ctx.output_dir  # used when no chat is in scope

    @function_tool
    def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
        """Read a UTF-8 text file under the hub directory.

        Args:
            path: Path relative to the hub root, or absolute (must resolve inside the hub).
            offset: Character offset to start reading from. Default 0.
            limit: Max characters to return. 0 = use the default cap (25_000).

        Returns:
            File contents. Large results are truncated with a hint pointing
            at an overflow file under output/<session>/ that holds the full text.
        """
        target = (hub_dir / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if hub_dir.resolve() not in target.parents and target != hub_dir.resolve():
            return f"[read_file refused: {path!r} is outside the hub directory]"
        if not target.is_file():
            return f"[read_file: {path!r} not found]"
        text = target.read_text(encoding="utf-8", errors="replace")
        if offset:
            text = text[offset:]
        if limit > 0:
            # Explicit user-requested slice. Honour it verbatim; no overflow noise.
            return text[:limit]
        out_dir = _artifact_dir(hub_dir, fallback_dir)
        body, _ = _truncate(
            text, cap=_READ_FILE_CAP, overflow_dir=out_dir, label="read", hub_dir=hub_dir
        )
        return body

    @function_tool
    def list_files(glob: str = "**/*") -> str:
        """List files under the hub directory matching a glob (default: all).

        Args:
            glob: Glob pattern relative to the hub root, e.g. "knowledge/*.md"
                or "raw_data/repo-a/**/*.py". Always scope a glob narrowly
                when listing a large folder like raw_data/.

        Returns:
            Newline-separated list of relative paths, capped at 100 entries.
            Footer hints at how to narrow if more entries exist.
        """
        matches = sorted(p for p in hub_dir.glob(glob) if p.is_file())
        if not matches:
            return ""
        rels = [str(p.relative_to(hub_dir)) for p in matches]
        if len(rels) <= _LIST_FILES_CAP:
            return "\n".join(rels)
        head = "\n".join(rels[:_LIST_FILES_CAP])
        return (
            head
            + f"\n\n[Showing {_LIST_FILES_CAP} of {len(rels)} matches. "
            + "Refine with a deeper path or a more specific glob "
            + "(e.g. 'raw_data/repo-a/src/**/*.py').]"
        )

    @function_tool
    def write_artifact(filename: str, content: str) -> str:
        """Save a file the user can download.

        Args:
            filename: Bare filename like "report.json". Any directory parts
                are stripped — the framework owns the path.
            content: UTF-8 text to write.

        Returns:
            A short markdown message with a clickable download link.
            Pass this through to the user; the link is what they need.
        """
        safe_name = _safe_filename(filename)
        if not safe_name:
            return f"[write_artifact refused: empty filename after sanitization]"

        target_dir = _artifact_dir(hub_dir, fallback_dir)
        target = (target_dir / safe_name).resolve()
        # Defence in depth: even after sanitization, never leave the dir.
        if target_dir.resolve() not in target.parents:
            return f"[write_artifact refused: {filename!r} escapes the artifacts directory]"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        size = target.stat().st_size

        url = _artifact_url(safe_name)
        size_label = _format_size(size)
        if url:
            return f"Saved **{safe_name}** ({size_label})\n\n[Download {safe_name}]({url})"
        return f"Saved **{safe_name}** ({size_label}) at `{target}`"

    @function_tool
    def list_artifacts() -> str:
        """List files saved in this chat so far."""
        target_dir = _artifact_dir(hub_dir, fallback_dir)
        if not target_dir.is_dir():
            return "(no artifacts yet)"
        files = sorted(p for p in target_dir.glob("*") if p.is_file())
        if not files:
            return "(no artifacts yet)"
        return "\n".join(f"- {p.name} ({_format_size(p.stat().st_size)})" for p in files)

    @function_tool
    def read_upload_full(filename: str) -> str:
        """Read the entire text content of an uploaded file.

        Use after a `read_upload` preview when you specifically need the
        whole file. Refuses binary uploads (images, generic blobs) — use
        a different tool for those. Caps at 250,000 characters; oversized
        files come through with a `[truncated]` marker.

        Args:
            filename: Bare filename of the upload, e.g. "report.json".

        Returns:
            Full UTF-8 text content, or a refusal note for binary files.
        """
        return _read_upload_full_impl(hub_dir, filename)

    @function_tool
    def read_upload(
        filename: str,
        offset: int = 1,
        limit: int = upload_previews.DEFAULT_TEXT_LIMIT_LINES,
        pages: str | None = None,
    ) -> str:
        """Read a file the user uploaded in this chat.

        Behaviour is type-aware. Text files default to the first 200 lines
        (whole file if small); JSON returns a structural summary plus the
        pretty head; CSV returns the header + first 20 rows + total count;
        PDFs extract the first 5 pages of text (use `pages` to page);
        images return metadata only; unknown binaries return a hex preview.

        Args:
            filename: Bare filename of the upload, e.g. "spec.pdf".
            offset: 1-indexed line to start at (text/json only). Default 1.
            limit: Max lines to return (text only). Default 200.
            pages: Page range for PDFs as "a-b" or "n" (1-indexed). Default "1-5".

        Returns:
            A preview formatted for the file's kind. Call `read_upload_full`
            when the preview's footer says more is available and you
            specifically need the rest.
        """
        return _read_upload_impl(hub_dir, filename, offset=offset, limit=limit, pages=pages)

    return [
        read_file,
        list_files,
        write_artifact,
        list_artifacts,
        read_upload,
        read_upload_full,
    ]


def _read_upload_full_impl(hub_dir: Path, filename: str) -> str:
    chat_id = _request_ctx.get_chat_id()
    if not chat_id:
        return "[read_upload_full: no chat is in scope]"
    upload_dir = memlib.chat_upload_dir(hub_dir, chat_id)
    safe_name = _safe_filename(filename)
    if not safe_name:
        return "[read_upload_full: invalid filename]"
    target = (upload_dir / safe_name).resolve()
    if upload_dir.resolve() not in target.parents:
        return f"[read_upload_full refused: {filename!r} escapes the uploads directory]"
    if not target.is_file():
        return f"[read_upload_full: {safe_name!r} not found; uploads: {_list_dir_names(upload_dir) or '(none)'}]"

    payload = target.read_bytes()
    meta = uploads_lib.read_meta(upload_dir, safe_name)
    if meta and isinstance(meta.get("kind"), str):
        kind = meta["kind"]
    else:
        mime = uploads_lib.guess_mime(safe_name)
        kind = uploads_lib.classify(mime, payload)
    if kind in ("binary", "image"):
        return (
            f"[read_upload_full refused: {safe_name!r} is a {kind} file. "
            f"This tool only reads text. Use read_upload for a metadata-only preview.]"
        )

    text = payload.decode("utf-8", errors="replace")
    cap = upload_previews.READ_FULL_MAX_CHARS
    if len(text) > cap:
        text = (
            text[:cap]
            + f"\n\n[End of preview: returned {cap} of {len(text)} characters "
            f"({len(payload)} bytes on disk). This is the upper bound for "
            f"read_upload_full. Use read_upload(filename, offset=N) for "
            f"line-based pagination beyond this if you need more.]"
        )
    return text


def _read_upload_impl(
    hub_dir: Path,
    filename: str,
    *,
    offset: int,
    limit: int,
    pages: str | None,
) -> str:
    chat_id = _request_ctx.get_chat_id()
    if not chat_id:
        return "[read_upload: no chat is in scope]"
    upload_dir = memlib.chat_upload_dir(hub_dir, chat_id)
    safe_name = _safe_filename(filename)
    if not safe_name:
        return "[read_upload: invalid filename]"
    target = (upload_dir / safe_name).resolve()
    if upload_dir.resolve() not in target.parents:
        return f"[read_upload refused: {filename!r} escapes the uploads directory]"
    if not target.is_file():
        return f"[read_upload: {safe_name!r} not found; uploads: {_list_dir_names(upload_dir) or '(none)'}]"

    payload = target.read_bytes()
    meta = uploads_lib.read_meta(upload_dir, safe_name)
    if meta and isinstance(meta.get("kind"), str):
        kind = meta["kind"]
        mime = meta.get("mime") or uploads_lib.guess_mime(safe_name)
    else:
        mime = uploads_lib.guess_mime(safe_name)
        kind = uploads_lib.classify(mime, payload)

    if kind == "image":
        # image_preview already includes the path.
        return upload_previews.image_preview(safe_name, mime=mime, size=len(payload), target=target)
    if kind == "binary":
        # binary_preview already includes the path.
        return upload_previews.binary_preview(safe_name, payload=payload, target=target)
    # All text-side previews get a path header so the agent can chain into
    # tools that take a path argument (extract_for_review, test_template,
    # validate_template, etc.) without an extra discovery step.
    if kind == "pdf":
        body = upload_previews.pdf_preview(payload, pages=pages)
    elif kind == "json":
        body = upload_previews.json_preview(payload)
    elif kind == "csv":
        body = upload_previews.csv_preview(payload)
    else:
        body = upload_previews.text_preview(payload, offset=offset, limit=limit)
    return _with_path_header(target, body)


def _with_path_header(target: Path, body: str) -> str:
    """Prefix a preview body with the upload's absolute path.

    Lets the agent pass the file straight to other tools (`extract_for_review`,
    `test_template`, etc.) without having to guess where uploads live. The
    binary/image preview functions already render their own path notes, so
    we only wrap text-side previews here.
    """
    return f"[Path on disk: {target}]\n\n{body}"


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------
def _artifact_dir(hub_dir: Path, fallback: Path) -> Path:
    chat_id = _request_ctx.get_chat_id()
    if chat_id:
        return memlib.chat_artifact_dir(hub_dir, chat_id)
    return fallback


def _artifact_url(filename: str) -> str | None:
    """Build a download URL for an artifact in the current chat.

    The URL embeds a short HMAC-signed token (``?t=<hex>``) so the browser
    can fetch the file by clicking the link in chat without sending a
    Bearer header. See `hubzoid._signing` for the token scheme.

    Pulls the public base URL from HUBZOID_PUBLIC_URL (operator override
    when the bridge is fronted by a reverse proxy). Falls back to the
    bridge's listen address derived from BRIDGE_PORT.

    Returns None when no chat is in scope — in that case the agent should
    show the user the local path instead (handled by the caller).
    """
    chat_id = _request_ctx.get_chat_id()
    if not chat_id:
        return None
    base = os.environ.get("HUBZOID_PUBLIC_URL", "").rstrip("/")
    if not base:
        port = os.environ.get("BRIDGE_PORT", "8000")
        base = f"http://127.0.0.1:{port}"
    token = _signing.sign_artifact_path(chat_id, filename)
    return (
        f"{base}/artifacts/{quote(chat_id, safe='')}/{quote(filename, safe='')}"
        f"?t={token}"
    )


def _safe_filename(raw: str) -> str:
    """Reduce to a bare filename. Strips directory parts and dangerous bits."""
    if not raw:
        return ""
    # Use posix-style basename; strip windows separators first.
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    # Disallow nulls, leading dots, anything that looks like a path.
    name = name.replace("\x00", "")
    if name in ("", ".", ".."):
        return ""
    return name


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _list_dir_names(d: Path) -> str:
    if not d.is_dir():
        return ""
    names = sorted(
        p.name
        for p in d.iterdir()
        if p.is_file() and not uploads_lib.is_sidecar(p.name)
    )
    return ", ".join(names)
