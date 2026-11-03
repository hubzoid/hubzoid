"""Native image vision: show a turn's images to the model as content blocks.

The images come as a list of names in the chat's uploads folder
(``_request_ctx.turn_images``), set by whoever built the prompt from the
message parts (``hubzoid.attachments``). This module turns them into real
multimodal content blocks at model-call time, per runtime:

  * claude-local (``claude_agent_sdk.query``): Anthropic image blocks, passed
    via the streaming-input message form.
  * OpenAI Agents (``Runner.run_streamed``) and codex-local: ``input_image``
    blocks (Responses API shape).

Design notes:
  - The uploads dir IS the store. Every turn's prompt is rebuilt from the
    stored parts, so an uploaded image stays visible for follow-ups.
  - Only files that resolve INSIDE the chat uploads dir *and* classify as an
    image are ever read. No arbitrary path is reachable — ``restricted/.env``
    is not an upload — so this never exposes secrets the way a filesystem Read
    tool would. That is the whole reason we inject rather than enable Read.
  - Optional Pillow resize keeps images token-bounded; without Pillow the bytes
    pass through (providers downscale large images anyway). Compression never
    raises: any failure falls back to the original bytes.
  - Compressed bytes are cached by content hash so a persisted image is not
    re-encoded on every turn.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from . import _request_ctx
from . import memory as memlib
from . import uploads as uploads_lib

# content-hash -> (mime, base64). Bounded so a long-lived process cannot grow it
# without limit. Cleared wholesale when full (simple, good enough for this size).
_CACHE: dict[str, tuple[str, str]] = {}
_CACHE_MAX = 64


def _safe_resolve(upload_dir: Path, name: str) -> Path | None:
    """Resolve ``name`` strictly inside ``upload_dir``. None if it escapes or is
    missing. References only ever carry basenames; a name with path parts is
    rejected rather than silently stripped."""
    if not name or Path(name).name != name:
        return None
    target = (upload_dir / name).resolve()
    if upload_dir.resolve() not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def _compress(payload: bytes, mime: str, *, max_edge: int) -> tuple[str, bytes]:
    """Resize so the long edge is <= max_edge, if Pillow is present and the image
    is larger. Otherwise return the bytes unchanged. Never raises."""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow optional
        return mime, payload
    try:
        im = Image.open(BytesIO(payload))
        w, h = im.size
        if max(w, h) <= max_edge:
            return mime, payload
        scale = max_edge / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        is_png = "png" in mime.lower()
        if not is_png and im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, format="PNG" if is_png else "JPEG")
        return ("image/png" if is_png else "image/jpeg"), buf.getvalue()
    except Exception:  # noqa: BLE001 — bad/unknown image -> send original
        return mime, payload


def _load_image(upload_dir: Path, name: str, *, max_edge: int) -> tuple[str, str] | None:
    """(mime, base64) for an uploaded image, or None if not found / not an image
    / escapes the uploads dir."""
    target = _safe_resolve(upload_dir, name)
    if target is None:
        return None
    payload = target.read_bytes()
    meta = uploads_lib.read_meta(upload_dir, target.name)
    if meta and isinstance(meta.get("mime"), str):
        mime = meta["mime"]
        kind = meta.get("kind") or uploads_lib.classify(mime, payload)
    else:
        mime = uploads_lib.guess_mime(target.name)
        kind = uploads_lib.classify(mime, payload)
    if kind != "image":
        return None
    key = f"{hashlib.sha256(payload).hexdigest()}:{max_edge}"
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    out_mime, data = _compress(payload, mime, max_edge=max_edge)
    result = (out_mime, base64.b64encode(data).decode())
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = result
    return result


def _resolved(
    names: list[str], hub_dir: Path, chat_id: str | None, *, max_edge: int, max_images: int
) -> list[tuple[str, str]]:
    """(mime, base64) for each image, capped to the most recent ``max_images``."""
    if not chat_id or not names:
        return []
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    if max_images > 0 and len(seen) > max_images:
        seen = seen[-max_images:]
    upload_dir = memlib.chat_upload_dir(hub_dir, chat_id)
    out: list[tuple[str, str]] = []
    for name in seen:
        img = _load_image(upload_dir, name, max_edge=max_edge)
        if img is not None:
            out.append(img)
    return out


def claude_prompt(
    prompt: str,
    hub_dir: Path,
    chat_id: str | None,
    *,
    enabled: bool,
    max_edge: int,
    max_images: int,
    resuming: bool = False,
):
    """Value to pass as ``query(prompt=...)``. The original string when there is
    nothing to inject, else an async iterable yielding one user message with the
    text plus Anthropic image blocks."""
    if not enabled:
        return prompt
    images = _resolved(_request_ctx.turn_images(resuming=resuming), hub_dir, chat_id,
                       max_edge=max_edge, max_images=max_images)
    if not images:
        return prompt
    content: list[dict] = [{"type": "text", "text": prompt}]
    for mime, b64 in images:
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
        )

    async def _gen():
        yield {"type": "user", "message": {"role": "user", "content": content}}

    return _gen()


def openai_input(
    prompt: str,
    hub_dir: Path,
    chat_id: str | None,
    *,
    enabled: bool,
    max_edge: int,
    max_images: int,
):
    """Value to pass as ``Runner.run_streamed(agent, input=...)``. The original
    string when there is nothing to inject, else a one-item input list with the
    text plus ``input_image`` blocks (Responses API shape)."""
    if not enabled:
        return prompt
    images = _resolved(_request_ctx.turn_images(), hub_dir, chat_id, max_edge=max_edge, max_images=max_images)
    if not images:
        return prompt
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for mime, b64 in images:
        content.append(
            {"type": "input_image", "image_url": f"data:{mime};base64,{b64}", "detail": "auto"}
        )
    return [{"role": "user", "content": content}]
