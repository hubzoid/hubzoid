"""Sharing a conversation: live collaborators and previewed snapshot links.

**Collaborators** (``hz_chat_members``, revisioned by ``hz_chat_member_sets``)
let named people read a conversation (viewer) or also write in it (editor).
Editors write on their own branches (``hubzoid.chat.engine``); nobody's line of
work is overwritten, and membership never lends anyone the owner's tools,
model or budget: every reply runs as the person who asked.

**Snapshot links** are previewed, then published bound to that preview's
digest, for an audience: named people, everyone signed in to the deployment,
or the public (only when ``sharing.public_enabled``). A snapshot is immutable;
narrowing the audience, turning copying off or shortening the expiry updates
its access state, widening needs a new preview and a new link. Revoking stops
every later read at once. The link token is 256 random bits; the database keeps
its hash (for lookups) and an encrypted copy only the owner can read back.

The projection keeps visible text, which tools ran and how they ended, and
author labels. It never carries reasoning, tool arguments or results, hidden
prompts or files (their names are listed as omissions); signed download links
in answer text become the file's name. A link stops when its owner is deleted
or no longer active. **Copying** a shared
conversation gives the reader an independent conversation with new ids, no
runs, no queue and no authority; it survives a later revoke.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from ..control import canonical
from . import graph as g
from .store import _dumps, _message, conversations, messages, new_id

metadata = sa.MetaData()
Err = g.GraphError
PREVIEW_SECONDS = 900


class SharingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    public_enabled: bool = False
    copy_enabled: bool = True
    export_enabled: bool = True
    presence_enabled: bool = True
    max_active_shares_per_conversation: int = Field(20, ge=1, le=100)
    max_snapshot_messages: int = Field(10000, ge=1, le=50000)
    max_snapshot_bytes: int = Field(10_485_760, ge=1, le=52_428_800)
    max_snapshot_files: int = Field(100, ge=1, le=500)
    max_total_file_bytes: int = Field(1_073_741_824, ge=1, le=5_368_709_120)
    max_live_members: int = Field(200, ge=1, le=1000)
    default_expiry_seconds: int | None = Field(None, ge=3600, le=31_536_000)


def register() -> None:
    from .config import namespaces, register_namespace

    ns = namespaces().get("sharing")
    if ns is None or not ns.installed:
        register_namespace("sharing", SharingSettings, owner="HUB-79")


register()

manifests = sa.Table(
    "hz_share_manifests", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("audience_type", sa.String(16), nullable=False),
    sa.Column("audience_json", sa.Text, nullable=False),
    sa.Column("snapshot_hash", sa.String(80), nullable=False),
    sa.Column("projection_json", sa.Text, nullable=False),
    sa.Column("source_graph_revision", sa.Integer, nullable=False),
    sa.Column("allow_copy", sa.Integer, nullable=False),
    sa.Column("expires_at", sa.Float),
    sa.Column("token_hash", sa.String(64), nullable=False),
    sa.Column("token_box", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("revoked_at", sa.Float),
    sa.UniqueConstraint("token_hash", name="uq_hz_share_token"),
)

previews = sa.Table(
    "hz_share_previews", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("conversation_id", sa.String(64), nullable=False),
    sa.Column("source_revision", sa.Integer, nullable=False),
    sa.Column("payload_hash", sa.String(80), nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
)

copies = sa.Table(
    "hz_share_copy_operations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("share_id", sa.String(64), nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("new_conversation_id", sa.String(64)),
    sa.Column("omissions_json", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
)

TABLES = (manifests, previews, copies)


def _owner_active(conn, hub_dir: Path, owner_id: str) -> bool:
    """The owner's account is active and not blocked."""
    from ..access import store_for
    from ..auth.schema import users

    row = conn.execute(sa.select(users.c.status, users.c.email).where(users.c.id == owner_id)).first()
    if row is None or row.status != "active":
        return False
    access = store_for(hub_dir)
    return not (access.is_suspended(row.email) or access.is_suspended(owner_id))


def owner_active(engine, hub_dir: Path, owner_id: str) -> bool:
    with engine.connect() as conn:
        return _owner_active(conn, hub_dir, owner_id)


def _now() -> float:
    return time.time()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# A download link the runtime signed for an agent-written file ("[Download
# report.md](https://…/artifacts/web-<id>/report.md?t=…)"). It works without
# signing in, so it never travels in a share, however it is spelt: percent-
# encoded, HTML-escaped or bare.
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_URLISH = re.compile(r"(?:https?://|/|%2[fF])[^\s<>\"'()\[\]]+")


def _decoded(url: str) -> str:
    out = url
    for _ in range(3):
        out = unquote(html.unescape(out))
    return out


def _signed(url: str) -> bool:
    parts = urlsplit(_decoded(url))
    return "/artifacts/" in parts.path.lower() and "t" in parse_qs(parts.query)


def _file_name(url: str) -> str:
    return urlsplit(_decoded(url)).path.rstrip("/").rsplit("/", 1)[-1] or "file"


def unlink(text: str) -> tuple[str, list[str]]:
    """``text`` without signed file links, and the names of the files left out.
    A link becomes its file name in plain text."""
    names: list[str] = []

    def link(m: re.Match) -> str:
        if not _signed(m.group(2)):
            return m.group(0)
        names.append(_file_name(m.group(2)))
        return names[-1]

    def bare(m: re.Match) -> str:
        if not _signed(m.group(0)):
            return m.group(0)
        names.append(_file_name(m.group(0)))
        return names[-1]

    text = _MD_LINK.sub(link, text)
    text = _URLISH.sub(bare, text)
    return text, names


def unlink_messages(msgs: list[dict]) -> tuple[list[dict], list[str]]:
    """Projected (or pre-2.0 snapshot) messages with signed file links removed
    from their text. Older snapshots stored them, so reads strip them too."""
    out, omitted = [], []
    for m in msgs:
        parts = []
        for p in m.get("content") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                text, names = unlink(p.get("text") or "")
                omitted += [f"file: {n}" for n in names]
                p = {**p, "text": text}
            parts.append(p)
        out.append({**m, "content": parts})
    return out, omitted


def project(msgs: list[dict], names: dict[str, str], *, pseudonyms: bool) -> tuple[list[dict], list[str]]:
    """The shareable form of a message path, and what was left out."""
    labels: dict[str, str] = {}
    out, omitted = [], set()
    for m in msgs:
        parts = []
        for p in m.get("content") or []:
            if not isinstance(p, dict):
                continue
            kind = p.get("type")
            if kind == "text":
                text, files = unlink(p.get("text") or "")
                omitted.update(f"file: {n}" for n in files)
                parts.append({"type": "text", "text": text})
            elif kind == "reasoning":
                omitted.add("reasoning")
            elif kind == "tool-call":
                status = (p.get("result") or {}).get("status") if isinstance(p.get("result"), dict) else None
                parts.append({"type": "tool-call", "toolCallId": p.get("toolCallId"), "toolName": p.get("toolName"),
                              **({"result": {"status": status}} if status else {})})
                omitted.add("tool arguments and results")
            elif kind in ("file", "image"):
                omitted.add(f"file: {p.get('name') or p.get('file_id')}")
            else:
                omitted.add("other content")
        author = m.get("author_id") or ""
        if m["role"] == "user":
            if pseudonyms:
                labels.setdefault(author, f"Participant {len(labels) + 1}")
                label = labels[author]
            else:
                label = names.get(author) or "Someone"
        else:
            label = None
        out.append({"id": m["id"], "role": m["role"], "content": parts, "status": m.get("status"),
                    "created_at": m.get("created_at"), "author": label,
                    **({"editor": True} if m.get("editor_id") else {})})
    return out, sorted(omitted)


class SharingService:
    def __init__(self, hub_dir: Path, engine, hub: str):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub

    def settings(self) -> dict:
        from .config import service_for

        return service_for(self.hub_dir).active().document["sharing"]

    # -- collaborators ---------------------------------------------------------------
    def collaborators(self, conv: dict, role: str) -> dict:
        with self.engine.connect() as conn:
            rev = conn.execute(sa.select(g.member_sets.c.revision).where(
                g.member_sets.c.conversation_id == conv["id"])).scalar() or 0
            rows = conn.execute(sa.select(g.members).where(g.members.c.conversation_id == conv["id"])
                                .order_by(g.members.c.created_at)).all()
        from ..auth import users

        out = []
        for r in rows:
            u = users.get(self.hub_dir, r.principal_id) if r.principal_type == "user" else None
            out.append({"principal_type": r.principal_type, "principal_id": r.principal_id, "role": r.role,
                        "email": (u or {}).get("email"), "name": (u or {}).get("name")})
        return {"revision": rev, "owner_id": conv["owner_id"], "members": out, "effective_role": role}

    def set_collaborators(self, conv: dict, actor_id: str, members_in: list, expected_revision: int) -> dict:
        from ..auth import users

        if conv["owner_id"] != actor_id:
            raise Err(403, "permission_denied", "Only the owner changes who can collaborate.")
        cfg = self.settings()
        if not cfg["enabled"]:
            raise Err(403, "feature_disabled", "Sharing is turned off for this deployment.")
        if not isinstance(members_in, list) or len(members_in) > cfg["max_live_members"]:
            raise Err(422, "member_limit", f"At most {cfg['max_live_members']} collaborators.")
        clean: dict[str, str] = {}
        for m in members_in:
            if not isinstance(m, dict) or m.get("role") not in ("editor", "viewer"):
                raise Err(422, "validation_failed", "Each member has a role: editor or viewer.")
            if m.get("principal_type", "user") != "user":
                raise Err(422, "validation_failed", "Only people can be added in this release.")
            ref = str(m.get("principal_id") or m.get("email") or "")
            u = users.get(self.hub_dir, ref) or users.find_by_email(self.hub_dir, ref)
            if u is None:
                raise Err(422, "principal_not_found", f"No account {ref!r} in this deployment.")
            if u["id"] == conv["owner_id"]:
                continue
            clean[u["id"]] = m["role"]
        t = _now()
        with self.engine.begin() as conn:
            cur = conn.execute(sa.select(g.member_sets.c.revision).where(
                g.member_sets.c.conversation_id == conv["id"])).scalar()
            if (cur or 0) != expected_revision:
                raise Err(409, "revision_conflict", "Collaborators changed. Reload and review again.",
                          current_revision=cur or 0)
            before = {(r.principal_id, r.role) for r in conn.execute(
                sa.select(g.members).where(g.members.c.conversation_id == conv["id"]))}
            conn.execute(g.members.delete().where(g.members.c.conversation_id == conv["id"]))
            for pid, role in clean.items():
                conn.execute(g.members.insert().values(conversation_id=conv["id"], principal_type="user",
                                                       principal_id=pid, role=role, added_by=actor_id, created_at=t))
            if cur is None:
                conn.execute(g.member_sets.insert().values(conversation_id=conv["id"], revision=1, updated_at=t))
            else:
                conn.execute(g.member_sets.update().where(g.member_sets.c.conversation_id == conv["id"])
                             .values(revision=cur + 1, updated_at=t))
            removed = {pid for pid, _ in before} - set(clean)
            # Someone removed stops sending: their queued messages here are parked.
            if removed:
                conn.execute(g.queue.update().where(g.queue.c.conversation_id == conv["id"],
                                                    g.queue.c.author_id.in_(removed),
                                                    g.queue.c.state.in_(("queued", "paused")))
                             .values(state="blocked", reason="access removed", updated_at=t))
            g.emit(conn, conv["id"], "collaborator.changed", conv["id"], {"revision": (cur or 0) + 1})
        return self.collaborators(conv, "owner")

    # -- snapshot links --------------------------------------------------------------
    def preview(self, conv: dict, actor_id: str, body: dict) -> dict:
        cfg = self.settings()
        if not cfg["enabled"]:
            raise Err(403, "feature_disabled", "Sharing is turned off for this deployment.")
        if conv["owner_id"] != actor_id:
            raise Err(403, "permission_denied", "Only the owner publishes links to this conversation.")
        audience = body.get("audience") or {"type": "deployment"}
        atype = audience.get("type")
        if atype not in ("named", "deployment", "public"):
            raise Err(422, "validation_failed", "audience.type is named, deployment or public.")
        if atype == "public" and not cfg["public_enabled"]:
            raise Err(403, "public_disabled", "Public links are off for this deployment.")
        principals = []
        if atype == "named":
            from ..auth import users

            for ref in audience.get("principals") or []:
                rid = ref.get("id") if isinstance(ref, dict) else ref
                u = users.get(self.hub_dir, str(rid)) or users.find_by_email(self.hub_dir, str(rid))
                if u is None:
                    raise Err(422, "principal_not_found", f"No account {rid!r} in this deployment.")
                principals.append(u["id"])
            if not principals:
                raise Err(422, "validation_failed", "Name at least one person for a named link.")
        allow_copy = bool(body.get("allow_copy", True)) and cfg["copy_enabled"]
        expires_at = body.get("expires_at")
        if expires_at is None and cfg["default_expiry_seconds"]:
            expires_at = _now() + cfg["default_expiry_seconds"]
        path_mode = body.get("path_mode", "selected")
        with self.engine.connect() as conn:
            rows = {r.id: _message(r) for r in conn.execute(sa.select(messages).where(
                messages.c.conversation_id == conv["id"], messages.c.deleted_at.is_(None)))}
            rev = conn.execute(sa.select(conversations.c.graph_revision).where(
                conversations.c.id == conv["id"])).scalar() or 0
        head = body.get("head_message_id") or conv.get("head_id")
        if path_mode == "all_branches":
            chosen = sorted(rows.values(), key=lambda m: (m["created_at"], m["id"]))
        else:
            chosen, node = [], head
            while node and node in rows:
                chosen.append(rows[node])
                node = rows[node].get("parent_id")
            chosen.reverse()
        chosen = [m for m in chosen if m["status"] != "running"]
        if not chosen:
            raise Err(422, "empty_snapshot", "Pick a finished message to share.")
        if len(chosen) > cfg["max_snapshot_messages"]:
            raise Err(413, "snapshot_too_large", f"A link holds at most {cfg['max_snapshot_messages']} messages.")
        from ..auth import users

        names = {}
        for m in chosen:
            a = m.get("author_id")
            if a and a not in names:
                names[a] = (users.get(self.hub_dir, a) or {}).get("name") or "Someone"
        projected, omitted = project(chosen, names, pseudonyms=atype == "public" and not body.get("show_names"))
        payload = {"conversation_id": conv["id"], "title": conv.get("title"), "agent": conv.get("agent"),
                   "messages": projected, "audience": {"type": atype, "principals": sorted(principals)},
                   "allow_copy": allow_copy, "expires_at": expires_at, "path_mode": path_mode,
                   "source_revision": rev, "omissions": omitted}
        if len(json.dumps(payload)) > cfg["max_snapshot_bytes"]:
            raise Err(413, "snapshot_too_large", "That snapshot is too large to share.")
        digest = canonical.digest(payload)
        pid = str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(previews.insert().values(id=pid, actor_id=actor_id, conversation_id=conv["id"],
                                                  source_revision=rev, payload_hash=digest,
                                                  payload_json=json.dumps(payload), expires_at=_now() + PREVIEW_SECONDS))
        return {"preview_id": pid, "digest": digest, "expires_in_seconds": PREVIEW_SECONDS, "projection": payload,
                "omissions": omitted, "source_revision": rev}

    def _idempotent(self, actor_id: str, route: str, key: str | None, body, change, kind: str):
        from ..control import deploy, idempotency

        return idempotency.run(self.engine, deployment_id=deploy.deployment_id(self.hub_dir), actor_id=actor_id,
                               route=route, key=key, body=body, change=change, hub_id=self.hub, kind=kind)

    def publish(self, actor_id: str, body: dict, *, key: str | None = None) -> dict:
        """Publish a reviewed preview (used once). With an Idempotency-Key a
        retry returns the same link; the token itself is never kept in the
        idempotency record, only the share id."""
        out = self._idempotent(actor_id, "POST /api/shares", key, body,
                               lambda conn: (201, {"id": self._publish(conn, actor_id, body)}), "chat.share.publish")
        with self.engine.connect() as conn:
            row = self._by_id(conn, out.body["id"])
        return self._manifest_json(row, with_token=True)

    def _publish(self, conn, actor_id: str, body: dict) -> str:
        pv = conn.execute(sa.select(previews).where(previews.c.id == body.get("preview_id"),
                                                    previews.c.actor_id == actor_id)).first()
        if pv is None:
            raise Err(404, "not_found", "That preview was not found, or it was already published.")
        if pv.expires_at < _now():
            raise Err(409, "preview_expired", "The preview expired. Preview again.")
        if pv.payload_hash != body.get("digest"):
            raise Err(409, "snapshot_changed", "Publish exactly the preview you reviewed.")
        rev = conn.execute(sa.select(conversations.c.graph_revision).where(
            conversations.c.id == pv.conversation_id)).scalar() or 0
        if rev != pv.source_revision:
            raise Err(409, "snapshot_changed", "The conversation changed since the preview. Preview again.")
        payload = json.loads(pv.payload_json)
        cfg = self.settings()
        if payload["audience"]["type"] == "public" and not cfg["public_enabled"]:
            raise Err(403, "public_disabled", "Public links are off for this deployment.")
        active = conn.execute(sa.select(sa.func.count()).select_from(manifests).where(
            manifests.c.conversation_id == pv.conversation_id, manifests.c.status == "active")).scalar() or 0
        if active >= cfg["max_active_shares_per_conversation"]:
            raise Err(429, "limit_reached", "This conversation has the most links allowed. Revoke one first.")
        from .. import secretbox

        token = secrets.token_urlsafe(32)
        sid = str(uuid.uuid4())
        conn.execute(manifests.insert().values(
            id=sid, hub_id=self.hub, conversation_id=pv.conversation_id, owner_id=actor_id, revision=1,
            status="active", audience_type=payload["audience"]["type"], audience_json=json.dumps(payload["audience"]),
            snapshot_hash=pv.payload_hash, projection_json=pv.payload_json, source_graph_revision=rev,
            allow_copy=1 if payload["allow_copy"] else 0, expires_at=payload["expires_at"],
            token_hash=token_hash(token), token_box=secretbox.encrypt(self.hub_dir, token), created_at=_now(),
            revoked_at=None))
        conn.execute(previews.delete().where(previews.c.id == pv.id))  # a preview publishes once
        return sid

    def _manifest_json(self, r, *, with_token: bool = False) -> dict:
        out = {"id": r.id, "conversation_id": r.conversation_id, "revision": r.revision, "status": r.status,
               "audience": json.loads(r.audience_json), "snapshot_hash": r.snapshot_hash,
               "allow_copy": bool(r.allow_copy), "expires_at": r.expires_at, "created_at": r.created_at}
        if with_token:
            from .. import appmode, secretbox

            token = secretbox.decrypt_text(self.hub_dir, r.token_box)
            out["url"] = f"{appmode.normalize_origin(appmode.public_url())}/s/{token}"
            out["token"] = token
        return out

    def list_shares(self, *, owner_id: str | None, conversation_id: str | None = None) -> list[dict]:
        q = sa.select(manifests).where(manifests.c.hub_id == self.hub)
        if owner_id is not None:
            q = q.where(manifests.c.owner_id == owner_id)
        if conversation_id:
            q = q.where(manifests.c.conversation_id == conversation_id)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(manifests.c.created_at.desc()).limit(200)).all()
        return [self._manifest_json(r, with_token=owner_id is not None and r.status == "active") for r in rows]

    def _by_id(self, conn, share_id: str, *, lock: bool = False):
        """A link of this hub only: managing another hub's links needs that hub."""
        q = sa.select(manifests).where(manifests.c.id == share_id, manifests.c.hub_id == self.hub)
        return conn.execute(q.with_for_update() if lock else q).first()

    def change(self, share_id: str, actor_id: str, body: dict, *, manager: bool) -> dict:
        """Narrow only: audience public -> deployment -> named, copy off, earlier expiry."""
        order = {"public": 3, "deployment": 2, "named": 1}
        with self.engine.begin() as conn:
            r = self._by_id(conn, share_id, lock=True)
            if r is None or (r.owner_id != actor_id and not manager):
                raise Err(404, "not_found", "That link was not found.")
            if r.revision != body.get("expected_revision"):
                raise Err(409, "revision_conflict", "That link changed. Reload it.", current_revision=r.revision)
            values: dict = {}
            if "audience" in body:
                aud = body["audience"]
                if order.get(aud.get("type"), 9) > order[r.audience_type]:
                    raise Err(422, "validation_failed", "A wider audience needs a new preview and a new link.")
                if aud["type"] == "named" and r.audience_type == "named":
                    old = set(json.loads(r.audience_json)["principals"])
                    if not set(aud.get("principals") or []) <= old:
                        raise Err(422, "validation_failed", "Adding people needs a new preview and a new link.")
                values.update(audience_type=aud["type"], audience_json=json.dumps(
                    {"type": aud["type"], "principals": sorted(aud.get("principals") or [])}))
            if body.get("allow_copy") is False:
                values["allow_copy"] = 0
            if "expires_at" in body:
                if r.expires_at is not None and (body["expires_at"] is None or body["expires_at"] > r.expires_at):
                    raise Err(422, "validation_failed", "Expiry can only be made earlier.")
                values["expires_at"] = body["expires_at"]
            conn.execute(manifests.update().where(manifests.c.id == share_id)
                         .values(revision=r.revision + 1, **values))
            return self._manifest_json(self._by_id(conn, share_id))

    def revoke(self, share_id: str, actor_id: str, expected_revision: int | None, *, manager: bool) -> None:
        with self.engine.begin() as conn:
            r = self._by_id(conn, share_id, lock=True)
            if r is None or (r.owner_id != actor_id and not manager):
                raise Err(404, "not_found", "That link was not found.")
            if r.status == "revoked":
                return
            if expected_revision is not None and r.revision != expected_revision:
                raise Err(409, "revision_conflict", "That link changed. Reload it.", current_revision=r.revision)
            conn.execute(manifests.update().where(manifests.c.id == share_id).values(
                status="revoked", revoked_at=_now(), revision=r.revision + 1))
            g.emit(conn, r.conversation_id, "share.revoked", share_id, {})

    # -- reading and copying ----------------------------------------------------------
    def readable(self, token: str, reader_id: str | None) -> dict:
        """The share behind a link, if this reader may open it now. Revoked,
        expired, unknown and not-for-you all look the same."""
        with self.engine.connect() as conn:
            return self._readable(conn, token, reader_id)

    def _readable(self, conn, token: str, reader_id: str | None, *, lock: bool = False) -> dict:
        q = sa.select(manifests).where(manifests.c.token_hash == token_hash(token))
        r = conn.execute(q.with_for_update() if lock else q).first()
        conv = conn.execute(sa.select(conversations.c.deleted_at, conversations.c.owner_id).where(
            conversations.c.id == r.conversation_id)).first() if r else None
        gone = Err(404, "not_found", "This shared conversation is unavailable.")
        if r is None or r.status != "active" or conv is None or conv.deleted_at is not None:
            raise gone
        if not _owner_active(conn, self.hub_dir, r.owner_id):
            raise gone  # a deleted, blocked or deactivated owner's links stop
        if r.expires_at is not None and r.expires_at < _now():
            raise gone
        if r.audience_type == "public":
            if not self.settings()["public_enabled"]:
                raise gone
        elif reader_id is None:
            raise Err(401, "unauthenticated", "Sign in to open this shared conversation.")
        elif r.audience_type == "named" and reader_id != r.owner_id and \
                reader_id not in json.loads(r.audience_json)["principals"]:
            raise gone
        projection = json.loads(r.projection_json)
        projection["messages"], files = unlink_messages(projection.get("messages") or [])
        if files:
            projection["omissions"] = sorted(set(projection.get("omissions") or []) | set(files))
        return {"row": r, "projection": projection}

    def view(self, token: str, reader_id: str | None) -> dict:
        got = self.readable(token, reader_id)
        r, p = got["row"], got["projection"]
        return {"title": p.get("title"), "agent": p.get("agent"), "messages": p["messages"],
                "audience": r.audience_type, "revision": r.revision, "allow_copy": bool(r.allow_copy)
                and reader_id is not None and self.settings()["copy_enabled"], "created_at": r.created_at,
                "omissions": p.get("omissions") or [], "kind": "snapshot"}

    def copy_preview(self, token: str, reader_id: str) -> dict:
        got = self.readable(token, reader_id)
        r, p = got["row"], got["projection"]
        if not r.allow_copy or not self.settings()["copy_enabled"]:
            raise Err(403, "copy_disabled", "Copying this conversation is not allowed.")
        payload = {"share": r.id, "share_revision": r.revision, "reader": reader_id,
                   "messages": len(p["messages"]), "omissions": p.get("omissions") or []}
        return {"digest": canonical.digest(payload), **payload,
                "note": "The copy is your own conversation. It stays yours even if this link is revoked."}

    def copy(self, token: str, reader_id: str, reader_email: str, agent: str, digest: str, *,
             key: str | None = None) -> dict:
        """An independent copy for the reader. Access, the link's revision and
        copy permission are checked again inside the copy's own transaction
        (the link row locked), so a revoke that lands first always wins. Each
        request makes its own copy; an Idempotency-Key makes a retry return it."""
        out = self._idempotent(reader_id, f"POST /api/shares/{token_hash(token)[:16]}/copies", key,
                               {"digest": digest},
                               lambda conn: (201, self._copy(conn, token, reader_id, reader_email, agent, digest)),
                               "chat.share.copy")
        return out.body

    def _copy(self, conn, token: str, reader_id: str, reader_email: str, agent: str, digest: str) -> dict:
        # Take the link's write lock first (a no-op update: SQLite's write lock,
        # PostgreSQL's row lock), so the access read below is the one that counts.
        conn.execute(manifests.update().where(manifests.c.token_hash == token_hash(token))
                     .values(revision=manifests.c.revision))
        got = self._readable(conn, token, reader_id, lock=True)
        r, p = got["row"], got["projection"]
        if not r.allow_copy or not self.settings()["copy_enabled"]:
            raise Err(403, "copy_disabled", "Copying this conversation is not allowed.")
        payload = {"share": r.id, "share_revision": r.revision, "reader": reader_id,
                   "messages": len(p["messages"]), "omissions": p.get("omissions") or []}
        if canonical.digest(payload) != digest:
            raise Err(409, "snapshot_changed", "The link changed. Review the copy again.")
        op_id = str(uuid.uuid4())
        cid = new_id("c_")
        t = _now()
        conn.execute(conversations.insert().values(
            id=cid, owner_id=reader_id, owner_email=reader_email, hub=self.hub, agent=agent,
            title=(p.get("title") or "Shared conversation") + " (copy)", title_source="user", archived=0,
            head_id=None, source="web", created_at=t, updated_at=t, graph_revision=0, temporary=0,
            expires_at=None, deleted_at=None))
        parent = None
        for m in p["messages"]:
            mid = new_id("m_")
            content = [x for x in m["content"] if x.get("type") == "text"]
            conn.execute(messages.insert().values(
                id=mid, conversation_id=cid, parent_id=parent, role=m["role"], content=_dumps(content),
                text="\n\n".join(x["text"] for x in content), status="complete", error=None, model=None,
                usage=None, created_at=m.get("created_at") or t, updated_at=t, branch_id=None, revises_id=None,
                author_id=reader_id, editor_id=None, run_id=None, requested_model=None, historical_only=1,
                context_changed=0, deleted_at=None))
            conn.execute(g.lineage.insert().values(new_message_id=mid, source_conversation_id=r.conversation_id,
                                                   source_message_id=m["id"], copy_operation_id=op_id, created_at=t))
            parent = mid
        g.ensure_initial_branch(conn, {"id": cid, "owner_id": reader_id}, reader_id)
        g.set_view(conn, cid, reader_id, None, parent)
        conn.execute(copies.insert().values(id=op_id, actor_id=reader_id, share_id=r.id, digest=digest,
                                            status="complete", new_conversation_id=cid,
                                            omissions_json=json.dumps(p.get("omissions") or []), error_code=None,
                                            created_at=t))
        return {"operation_id": op_id, "status": "complete", "new_conversation_id": cid}

    def operation(self, op_id: str, actor_id: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(copies).where(copies.c.id == op_id, copies.c.actor_id == actor_id)).first()
        if r is None:
            raise Err(404, "not_found", "That operation was not found.")
        return {"operation_id": r.id, "status": r.status, "new_conversation_id": r.new_conversation_id,
                "omissions": json.loads(r.omissions_json or "[]")}
