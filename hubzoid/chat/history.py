"""The model prompt for one turn: the active branch, flattened as in 1.0.x.

The branch runs from the conversation's root to the new user message. Each
message becomes a ``[user]`` or ``[assistant]`` block through the bridge's own
``server._flatten_messages``, so a web app turn reads exactly like an
OpenAI-compatible one. Assistant messages contribute their answer text only
(never reasoning or tool entries); a run's result posted to the chat
(``workflows.chat_link``) contributes its form fenced as data, as a person's
decision (``request_outcome``) does for user text. A user message's attachments come first in
its block, as the notes ``attachments.note`` writes, built from the message's
stored parts. Its images also go to the runtime as a list (``branch_images``),
which ``hubzoid.vision_inject`` shows to the model. Files live in the
conversation's uploads folder (``memory.chat_upload_dir`` of its
``store.chat_key``), whose folder is the turn's chat scope.
"""
from __future__ import annotations

from pathlib import Path

from ..attachments import RASTER_IMAGES, describe_parts  # noqa: F401 - RASTER_IMAGES is re-exported

__all__ = ["RASTER_IMAGES", "user_text", "assistant_text", "build_prompt", "branch_images", "message_images"]


def user_text(hub_dir: Path, key: str, content: list) -> str:
    """A user message as prompt text; ``key`` is the conversation's ``store.chat_key``."""
    notes, _ = describe_parts(hub_dir, key, content)
    texts: list[str] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            if part.get("text"):
                texts.append(str(part["text"]))
        elif kind == "request_outcome" and part.get("for_model"):
            # The person's answer to the agent's approval or question, as data.
            texts = [str(part["for_model"])]
    return "\n\n".join(notes + ["\n\n".join(texts)] if texts else notes)


def message_images(hub_dir: Path, key: str, content: list) -> list[str]:
    """The images of one user message, as names in the chat's uploads folder."""
    return describe_parts(hub_dir, key, content)[1]


def branch_images(hub_dir: Path, key: str, branch: list[dict]) -> list[str]:
    """Every image the person attached on this branch, oldest first."""
    out: list[str] = []
    for message in branch:
        if message.get("role") == "user":
            out += message_images(hub_dir, key, message.get("content"))
    return out


def assistant_text(content: list) -> str:
    parts = [p for p in content or [] if isinstance(p, dict)]
    for part in parts:
        if part.get("type") == "run_result" and part.get("for_model"):
            return str(part["for_model"])  # a run's posted result, fenced as data
    return "".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")


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
