"""Pre-2.0 read-only links (``hz_shares``, one per conversation).

  GET    /api/conversations/{id}/share   the owner's pre-2.0 link, if any: {share_id, url}
  DELETE /api/conversations/{id}/share   turn that link off (204)

New links are previewed snapshots (``hubzoid.chat.sharing``); there is no way
to make or refresh a pre-2.0 link any more, because it skipped the preview, the
sharing settings and the omission rules. Existing ones stay readable at
``GET /api/shares/{id}`` for signed-in people while their owner is active, with
reasoning and signed download links removed (``sharing_routes._legacy``).
"""
from __future__ import annotations

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import appmode
from ..auth import users
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

    @app.delete("/api/conversations/{conv_id}/share", status_code=204)
    async def delete_share(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        await db(store.delete_shares, conv["id"])
        return Response(status_code=204)
