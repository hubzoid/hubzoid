"""The model prompt for one turn: the active branch, flattened as in 1.0.x.

The branch runs from the conversation's root to the new user message. Each
message becomes a ``[user]`` or ``[assistant]`` block through the bridge's own
``server._flatten_messages``, so a web app turn reads exactly like an
OpenAI-compatible one. Assistant messages contribute their answer text only
(never reasoning or tool entries). A user message's attachments come first in
its block, as the notes ``server._attachment_note`` writes: an image becomes
``[Image: name]``, which ``hubzoid.vision_inject`` shows to the model, and any
other file points the agent at ``read_upload``. Files live in the
conversation's uploads folder (``memory.chat_upload_dir`` of its
``store.chat_key``), whose folder is the turn's chat scope.
"""
from __future__ import annotations

from pathlib import Path

from .. import memory as memlib

# Pictures the models take directly. Other image types (SVG, TIFF, HEIC ...)
# are offered to the agent as files.
RASTER_IMAGES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def attachment_note(name: str, size: int, mime: str, target: Path, *, image: bool) -> str:
    from .. import server

    if not target.is_file():
        return (f"[Attachment unreadable: the user attached {name} but it is no longer "
                f"available. Tell them and ask them to upload it again. Do NOT guess its "
                f"contents.]")
    if image or not mime.startswith("image/"):
        return server._attachment_note(name, size, mime, target)
    # A picture format the model cannot take: the file note, with its real type.
    note = server._attachment_note(name, size, "application/octet-stream", target)
    return note.replace("application/octet-stream", mime, 1)


def user_text(hub_dir: Path, key: str, content: list) -> str:
    """A user message as prompt text; ``key`` is the conversation's ``store.chat_key``."""
    upload_dir = memlib.chat_upload_dir(Path(hub_dir), key)
    notes: list[str] = []
    texts: list[str] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            if part.get("text"):
                texts.append(str(part["text"]))
        elif kind in ("file", "image") and part.get("file_id"):
            name = str(part["file_id"])
            notes.append(attachment_note(
                name, int(part.get("size") or 0),
                str(part.get("mime") or "application/octet-stream"),
                upload_dir / name, image=kind == "image"))
    return "\n\n".join(notes + ["\n\n".join(texts)] if texts else notes)


def assistant_text(content: list) -> str:
    return "".join(str(p.get("text") or "") for p in content or []
                   if isinstance(p, dict) and p.get("type") == "text")


def build_prompt(hub_dir: Path, key: str, branch: list[dict]) -> str:
    """The flattened prompt for a branch of stored messages (root first) of the
    conversation whose ``store.chat_key`` is ``key``."""
    from .. import server

    turns: list[dict] = []
    for message in branch:
        role = message.get("role")
        if role == "user":
            turns.append({"role": "user",
                          "content": user_text(hub_dir, key, message.get("content"))})
        elif role == "assistant":
            turns.append({"role": "assistant", "content": assistant_text(message.get("content"))})
    return server._flatten_messages(turns)
