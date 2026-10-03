"""Read-only shares of a conversation.

  GET    /api/conversations/{id}/share   the owner's current link: {share_id, url}
  POST   /api/conversations/{id}/share   snapshot the active branch, return the link
  DELETE /api/conversations/{id}/share   revoke the link (204)
  GET    /api/shares/{share_id}          any signed-in person of the deployment

A share is a snapshot of the active branch (root to ``head_id``) taken when the
owner shares, so later edits never reach a link someone already opened. Sharing
again refreshes the snapshot behind the same link. The snapshot keeps answer
text, reasoning, which tools ran and how they ended (not their arguments), and
attachment names and types, never a way to download the attachments.
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import appmode
from .common import ChatContext, db, error, not_found


def public_parts(content: list) -> list[dict]:
    out: list[dict] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "reasoning"):
            out.append({"type": kind, "text": part.get("text") or ""})
        elif kind == "tool-call":
            item = {"type": "tool-call", "toolCallId": part.get("toolCallId"),
                    "toolName": part.get("toolName")}
            if isinstance(part.get("result"), dict):
                item["result"] = {"status": part["result"].get("status")}
            out.append(item)
        elif kind in ("file", "image"):
            out.append({"type": kind, "name": part.get("name") or part.get("file_id"),
                        "mime": part.get("mime"), "size": part.get("size")})
    return out


def snapshot(store, conv: dict, owner_name: str) -> dict:
    head = conv.get("head_id") or store.latest_leaf(conv["id"])
    messages = [{"id": m["id"], "role": m["role"], "content": public_parts(m.get("content")),
                 "status": m.get("status"), "created_at": m.get("created_at")}
                for m in store.branch(conv["id"], head)]
    return {"title": conv.get("title"), "agent": conv.get("agent"), "owner_name": owner_name,
            "created_at": time.time(), "messages": messages}


def share_url(share_id: str) -> str:
    # A gateway bridge's public URL includes /b/<hub> for its API and files.
    # Shared conversations are pages of the deployment's app at /s/<id>.
    return f"{appmode.normalize_origin(appmode.public_url())}/s/{share_id}"


def _disabled_import(share: dict) -> bool:
    snap = share.get("snapshot") or {}
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            return True
    if not isinstance(snap, dict):
        return True
    return snap.get("source") == "openwebui" and snap.get("audience") != "signed_in"


def register(app: FastAPI, ctx: ChatContext) -> None:
    store = ctx.store

    @app.get("/api/conversations/{conv_id}/share")
    async def get_share(conv_id: str, request: Request):
        user = await ctx.user(request)
        conv = await ctx.owned(user, conv_id)
        share = await db(store.share_for, conv["id"])
        if share is None or _disabled_import(share):
            raise error(404, "not_shared", "This conversation is not shared.")
        return {"share_id": share["id"], "url": share_url(share["id"])}

    @app.post("/api/conversations/{conv_id}/share")
    async def create_share(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        snap = await db(snapshot, store, conv, user.name or user.email)
        if not snap["messages"]:
            raise error(409, "nothing_to_share", "Send a message before sharing this conversation.")
        share = await db(store.save_share, conversation_id=conv["id"], owner_id=user.id,
                         title=conv.get("title"), agent=conv.get("agent"), snapshot=snap)
        return JSONResponse({"share_id": share["id"], "url": share_url(share["id"])})

    @app.delete("/api/conversations/{conv_id}/share", status_code=204)
    async def delete_share(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        await db(store.delete_shares, conv["id"])
        return Response(status_code=204)

    @app.get("/api/shares/{share_id}")
    async def read_share(share_id: str, request: Request):
        await ctx.user(request)
        share = await db(store.get_share, share_id) if 8 <= len(share_id) <= 64 else None
        if share is None or _disabled_import(share):
            raise not_found("shared conversation")
        snap = share.get("snapshot") or {}
        return {"title": snap.get("title"), "agent": snap.get("agent"),
                "owner_name": snap.get("owner_name"), "created_at": snap.get("created_at"),
                "messages": snap.get("messages") or []}
