"""Personal organisation of conversations: folders, pins, tags, bookmarks, archive.

Everything here is one person's view: it never changes anyone else's list,
never shares anything and never grants access. A folder may carry defaults
(``prompt_ids``, ``file_ids``, ``model_ids``, ``parameters``) that inherit from
root to leaf: a missing key inherits, an explicit ``[]`` clears, a list
replaces, and parameters merge per key with ``null`` removing one.
``resolve_context`` returns the effective values, which folder each came from
and a digest that a queued message keeps, so a later folder edit never changes
work already admitted.

A manifest (``version: 1``, ``scope: self``) applies folders, tags,
placements and bookmarks in one transaction after a dry run shows its diff
and digest; deletion is never inferred from absence.
"""
from __future__ import annotations

import json
import time
import unicodedata
import uuid
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..control import canonical
from . import graph as g
from .store import conversations, messages

metadata = sa.MetaData()
Err = g.GraphError


class OrganizationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_folders: int = Field(500, ge=1, le=5000)
    max_depth: int = Field(8, ge=1, le=16)
    max_tags: int = Field(200, ge=1, le=2000)
    max_bookmarks: int = Field(5000, ge=1, le=50000)
    bulk_limit: int = Field(100, ge=1, le=500)


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    default_types: list[str] = Field(default_factory=lambda: ["conversation", "message"])
    page_size: int = Field(20, ge=1, le=100)
    max_page_size: int = Field(100, ge=1, le=100)
    query_max_chars: int = Field(512, ge=1, le=2048)
    timeout_ms: int = Field(2000, ge=100, le=10000)
    index_body_max_bytes: int = Field(1_048_576, ge=1024, le=16_777_216)

    @model_validator(mode="after")
    def _page(self):
        if self.page_size > self.max_page_size:
            raise ValueError("search.page_size can't exceed search.max_page_size")
        return self


def register() -> None:
    from .config import namespaces, register_namespace

    for name, model in (("organization", OrganizationSettings), ("search", SearchSettings)):
        ns = namespaces().get(name)
        if ns is None or not ns.installed:
            register_namespace(name, model, owner="HUB-73")


register()

folders = sa.Table(
    "hz_chat_folders", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("owner_id", sa.String(64), nullable=False, index=True),
    sa.Column("parent_id", sa.String(64)),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("normalized_name", sa.String(255), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("defaults_json", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("hub_id", "owner_id", "parent_id", "normalized_name", name="uq_hz_folder_name"),
)

placements = sa.Table(
    "hz_chat_user_organization", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("folder_id", sa.String(64)),
    sa.Column("pinned_rank", sa.Integer),
    sa.Column("archived", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

tags = sa.Table(
    "hz_chat_tags", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False, index=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("normalized_name", sa.String(255), nullable=False),
    sa.Column("color", sa.String(32), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("hub_id", "user_id", "normalized_name", name="uq_hz_tag_name"),
)

conversation_tags = sa.Table(
    "hz_chat_conversation_tags", metadata,
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), primary_key=True),
    sa.Column("tag_id", sa.String(64), primary_key=True),
)

bookmarks = sa.Table(
    "hz_chat_bookmarks", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False, index=True),
    sa.Column("conversation_id", sa.String(64), nullable=False),
    sa.Column("message_id", sa.String(64)),
    sa.Column("branch_id", sa.String(64)),
    sa.Column("label", sa.String(255), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

state = sa.Table(
    "hz_chat_organization_state", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
)

TABLES = (folders, placements, tags, conversation_tags, bookmarks, state)
COLORS = ("neutral", "saffron", "teal", "indigo", "rose", "green", "amber", "slate")
DEFAULT_KEYS = ("prompt_ids", "file_ids", "model_ids", "parameters")


def _now() -> float:
    return time.time()


def normalize_name(name: Any, what: str = "name") -> tuple[str, str]:
    if not isinstance(name, str):
        raise Err(422, "validation_failed", f"{what} is text.")
    clean = unicodedata.normalize("NFC", " ".join(name.split()))
    if not 1 <= len(clean) <= 80 or any(unicodedata.category(c) == "Cc" for c in clean):
        raise Err(422, "validation_failed", f"A {what} is 1 to 80 characters.")
    return clean, clean.casefold()


def _defaults(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_KEYS):
        raise Err(422, "validation_failed", f"defaults keys are among: {', '.join(DEFAULT_KEYS)}.")
    for key in ("prompt_ids", "file_ids", "model_ids"):
        if key in value and (not isinstance(value[key], list) or not all(isinstance(x, str) for x in value[key])):
            raise Err(422, "validation_failed", f"defaults.{key} is a list of ids.")
    if "parameters" in value and not isinstance(value["parameters"], dict):
        raise Err(422, "validation_failed", "defaults.parameters is an object.")
    return value


class OrganizationService:
    def __init__(self, engine, hub: str, hub_dir):
        self.engine = engine
        self.hub = hub
        self.hub_dir = hub_dir

    def settings(self) -> dict:
        from .config import service_for

        return service_for(self.hub_dir).active().document["organization"]

    def _bump(self, conn, user_id: str) -> int:
        cur = conn.execute(sa.select(state.c.revision).where(state.c.hub_id == self.hub,
                                                            state.c.user_id == user_id)).scalar()
        if cur is None:
            conn.execute(state.insert().values(hub_id=self.hub, user_id=user_id, revision=1))
            return 1
        conn.execute(state.update().where(state.c.hub_id == self.hub, state.c.user_id == user_id)
                     .values(revision=cur + 1))
        return cur + 1

    # -- reads ------------------------------------------------------------------------
    def export(self, user_id: str) -> dict:
        with self.engine.connect() as conn:
            rev = conn.execute(sa.select(state.c.revision).where(state.c.hub_id == self.hub,
                                                                state.c.user_id == user_id)).scalar() or 0
            f = [self._folder_json(r) for r in conn.execute(sa.select(folders).where(
                folders.c.hub_id == self.hub, folders.c.owner_id == user_id).order_by(folders.c.position))]
            t = [self._tag_json(r) for r in conn.execute(sa.select(tags).where(
                tags.c.hub_id == self.hub, tags.c.user_id == user_id).order_by(tags.c.position))]
            ct: dict[str, list[str]] = {}
            for r in conn.execute(sa.select(conversation_tags).where(conversation_tags.c.user_id == user_id)):
                ct.setdefault(r.conversation_id, []).append(r.tag_id)
            p = [{"conversation_id": r.conversation_id, "folder_id": r.folder_id, "pinned_position": r.pinned_rank,
                  "archived": bool(r.archived), "tag_ids": sorted(ct.get(r.conversation_id, [])),
                  "revision": r.revision}
                 for r in conn.execute(sa.select(placements).where(placements.c.hub_id == self.hub,
                                                                  placements.c.user_id == user_id))]
            b = [self._bookmark_json(r) for r in conn.execute(sa.select(bookmarks).where(
                bookmarks.c.hub_id == self.hub, bookmarks.c.user_id == user_id).order_by(bookmarks.c.position))]
        return {"revision": rev, "folders": f, "tags": t, "placements": p, "bookmarks": b, "next_cursor": None}

    @staticmethod
    def _folder_json(r) -> dict:
        return {"id": r.id, "name": r.name, "parent_id": r.parent_id, "position": r.position,
                "defaults": json.loads(r.defaults_json or "{}"), "revision": r.revision}

    @staticmethod
    def _tag_json(r) -> dict:
        return {"id": r.id, "name": r.name, "color": r.color, "position": r.position, "revision": r.revision}

    @staticmethod
    def _bookmark_json(r) -> dict:
        return {"id": r.id, "conversation_id": r.conversation_id, "message_id": r.message_id, "branch_id": r.branch_id,
                "label": r.label, "position": r.position, "revision": r.revision}

    # -- folders ----------------------------------------------------------------------
    def _depth(self, conn, user_id: str, parent_id: str | None, moving: str | None = None) -> int:
        depth, node, seen = 0, parent_id, set()
        while node is not None:
            if node == moving or node in seen:
                raise Err(409, "validation_failed", "A folder can't be moved inside itself.")
            seen.add(node)
            row = conn.execute(sa.select(folders.c.parent_id, folders.c.owner_id).where(folders.c.id == node)).first()
            if row is None or row.owner_id != user_id:
                raise Err(404, "not_found", "That folder was not found.")
            node = row.parent_id
            depth += 1
        return depth

    def create_folder(self, user_id: str, body: dict) -> dict:
        cfg = self.settings()
        name, norm = normalize_name(body.get("name"), "folder name")
        parent = body.get("parent_id")
        with self.engine.begin() as conn:
            count = conn.execute(sa.select(sa.func.count()).select_from(folders).where(
                folders.c.hub_id == self.hub, folders.c.owner_id == user_id)).scalar() or 0
            if count >= cfg["max_folders"]:
                raise Err(429, "limit_reached", f"You have the most folders allowed ({cfg['max_folders']}).")
            if self._depth(conn, user_id, parent) + 1 > cfg["max_depth"]:
                raise Err(422, "validation_failed", f"Folders nest at most {cfg['max_depth']} deep.")
            if conn.execute(sa.select(folders.c.id).where(
                    folders.c.hub_id == self.hub, folders.c.owner_id == user_id, folders.c.parent_id.is_(parent)
                    if parent is None else folders.c.parent_id == parent, folders.c.normalized_name == norm)).first():
                raise Err(409, "validation_failed", "A folder with that name is already here.")
            fid = body.get("id") or str(uuid.uuid4())
            pos = body.get("position")
            if pos is None:
                pos = (conn.execute(sa.select(sa.func.max(folders.c.position)).where(
                    folders.c.owner_id == user_id, folders.c.hub_id == self.hub)).scalar() or 0) + 1
            t = _now()
            conn.execute(folders.insert().values(id=fid, hub_id=self.hub, owner_id=user_id, parent_id=parent, name=name,
                                                 normalized_name=norm, position=int(pos),
                                                 defaults_json=json.dumps(_defaults(body.get("defaults"))),
                                                 revision=1, created_at=t, updated_at=t))
            self._bump(conn, user_id)
            return self._folder_json(conn.execute(sa.select(folders).where(folders.c.id == fid)).first())

    def update_folder(self, user_id: str, folder_id: str, body: dict) -> dict:
        with self.engine.begin() as conn:
            r = conn.execute(sa.select(folders).where(folders.c.id == folder_id)).first()
            if r is None or r.owner_id != user_id:
                raise Err(404, "not_found", "That folder was not found.")
            if r.revision != body.get("expected_revision"):
                raise Err(409, "revision_conflict", "That folder changed. Reload it.", current_revision=r.revision)
            values: dict = {}
            if "name" in body:
                values["name"], values["normalized_name"] = normalize_name(body["name"], "folder name")
            if "parent_id" in body:
                if self._depth(conn, user_id, body["parent_id"], moving=folder_id) + 1 > self.settings()["max_depth"]:
                    raise Err(422, "validation_failed", "That would nest folders too deep.")
                values["parent_id"] = body["parent_id"]
            if "position" in body:
                values["position"] = int(body["position"])
            if "defaults" in body:
                values["defaults_json"] = json.dumps(_defaults(body["defaults"]))
            conn.execute(folders.update().where(folders.c.id == folder_id)
                         .values(revision=r.revision + 1, updated_at=_now(), **values))
            self._bump(conn, user_id)
            return self._folder_json(conn.execute(sa.select(folders).where(folders.c.id == folder_id)).first())

    def delete_folder(self, user_id: str, folder_id: str, body: dict) -> None:
        disposition = body.get("disposition", "move_to_parent")
        if disposition not in ("move_to_parent",):
            raise Err(422, "validation_failed", "disposition is move_to_parent (conversations are never deleted "
                                                "with a folder in this release).")
        with self.engine.begin() as conn:
            r = conn.execute(sa.select(folders).where(folders.c.id == folder_id)).first()
            if r is None or r.owner_id != user_id:
                raise Err(404, "not_found", "That folder was not found.")
            if r.revision != body.get("expected_revision"):
                raise Err(409, "revision_conflict", "That folder changed. Reload it.", current_revision=r.revision)
            conn.execute(folders.update().where(folders.c.parent_id == folder_id).values(parent_id=r.parent_id))
            conn.execute(placements.update().where(placements.c.folder_id == folder_id,
                                                   placements.c.user_id == user_id).values(folder_id=r.parent_id))
            conn.execute(folders.delete().where(folders.c.id == folder_id))
            self._bump(conn, user_id)

    def resolve_context(self, user_id: str, folder_id: str | None, overrides: dict | None = None) -> dict:
        """Effective folder defaults, each value's source, and their digest."""
        chain = []
        with self.engine.connect() as conn:
            node, seen = folder_id, set()
            while node is not None and node not in seen:
                seen.add(node)
                r = conn.execute(sa.select(folders).where(folders.c.id == node)).first()
                if r is None or r.owner_id != user_id:
                    raise Err(404, "not_found", "That folder was not found.")
                chain.append(r)
                node = r.parent_id
        effective: dict = {}
        provenance: dict = {}
        for r in reversed(chain):  # root to leaf
            d = json.loads(r.defaults_json or "{}")
            for key in ("prompt_ids", "file_ids", "model_ids"):
                if key in d:
                    effective[key] = list(d[key])
                    provenance[key] = r.id
            for k, v in (d.get("parameters") or {}).items():
                params = effective.setdefault("parameters", {})
                if v is None:
                    params.pop(k, None)
                else:
                    params[k] = v
                provenance[f"parameters.{k}"] = r.id
        for key, value in (overrides or {}).items():
            effective[key] = value
            provenance[key] = "conversation"
        return {"effective": effective, "provenance": provenance, "context_digest": canonical.digest(effective),
                "blocked_references": []}

    # -- tags ---------------------------------------------------------------------------
    def create_tag(self, user_id: str, body: dict) -> dict:
        name, norm = normalize_name(body.get("name"), "tag name")
        color = body.get("color", "neutral")
        if color not in COLORS:
            raise Err(422, "validation_failed", f"color is one of: {', '.join(COLORS)}.")
        with self.engine.begin() as conn:
            count = conn.execute(sa.select(sa.func.count()).select_from(tags).where(
                tags.c.hub_id == self.hub, tags.c.user_id == user_id)).scalar() or 0
            if count >= self.settings()["max_tags"]:
                raise Err(429, "limit_reached", "You have the most tags allowed.")
            if conn.execute(sa.select(tags.c.id).where(tags.c.hub_id == self.hub, tags.c.user_id == user_id,
                                                       tags.c.normalized_name == norm)).first():
                raise Err(409, "validation_failed", "You already have a tag with that name.")
            tid = body.get("id") or str(uuid.uuid4())
            t = _now()
            conn.execute(tags.insert().values(id=tid, hub_id=self.hub, user_id=user_id, name=name, normalized_name=norm,
                                              color=color, position=count + 1, revision=1, created_at=t, updated_at=t))
            self._bump(conn, user_id)
            return self._tag_json(conn.execute(sa.select(tags).where(tags.c.id == tid)).first())

    def delete_tag(self, user_id: str, tag_id: str, expected_revision: int) -> None:
        with self.engine.begin() as conn:
            r = conn.execute(sa.select(tags).where(tags.c.id == tag_id)).first()
            if r is None or r.user_id != user_id:
                raise Err(404, "not_found", "That tag was not found.")
            if r.revision != expected_revision:
                raise Err(409, "revision_conflict", "That tag changed.", current_revision=r.revision)
            conn.execute(conversation_tags.delete().where(conversation_tags.c.tag_id == tag_id))
            conn.execute(tags.delete().where(tags.c.id == tag_id))
            self._bump(conn, user_id)

    # -- placements and bookmarks -----------------------------------------------------------
    def organize(self, user_id: str, conversation_id: str, body: dict, *, readable) -> dict:
        readable(conversation_id)
        with self.engine.begin() as conn:
            return self._organize(conn, user_id, conversation_id, body)

    def _organize(self, conn, user_id: str, conversation_id: str, body: dict) -> dict:
        r = conn.execute(sa.select(placements).where(placements.c.hub_id == self.hub, placements.c.user_id == user_id,
                                                     placements.c.conversation_id == conversation_id)).first()
        current = r.revision if r else 0
        if body.get("expected_revision", current) != current:
            raise Err(409, "revision_conflict", "This conversation's place changed. Reload.", current_revision=current)
        folder_id = body.get("folder_id", r.folder_id if r else None)
        if folder_id is not None:
            f = conn.execute(sa.select(folders.c.owner_id).where(folders.c.id == folder_id)).first()
            if f is None or f.owner_id != user_id:
                raise Err(404, "not_found", "That folder was not found.")
        values = dict(folder_id=folder_id,
                      pinned_rank=body.get("pinned_position", r.pinned_rank if r else None),
                      archived=1 if body.get("archived", bool(r.archived) if r else False) else 0,
                      revision=current + 1, updated_at=_now())
        if r is None:
            conn.execute(placements.insert().values(hub_id=self.hub, user_id=user_id, conversation_id=conversation_id,
                                                    **values))
        else:
            conn.execute(placements.update().where(placements.c.hub_id == self.hub, placements.c.user_id == user_id,
                                                   placements.c.conversation_id == conversation_id).values(**values))
        if "tag_ids" in body:
            mine = {x.id for x in conn.execute(sa.select(tags.c.id).where(tags.c.user_id == user_id))}
            if not set(body["tag_ids"]) <= mine:
                raise Err(404, "not_found", "One of those tags was not found.")
            conn.execute(conversation_tags.delete().where(conversation_tags.c.user_id == user_id,
                                                          conversation_tags.c.conversation_id == conversation_id))
            for tid in body["tag_ids"]:
                conn.execute(conversation_tags.insert().values(user_id=user_id, conversation_id=conversation_id,
                                                               tag_id=tid))
        # the owner's own archive flag stays where the conversation list already reads it
        conn.execute(conversations.update().where(conversations.c.id == conversation_id,
                                                  conversations.c.owner_id == user_id)
                     .values(archived=values["archived"]))
        self._bump(conn, user_id)
        return {"conversation_id": conversation_id, **{k: v for k, v in values.items() if k != "updated_at"},
                "archived": bool(values["archived"]), "pinned_position": values["pinned_rank"],
                "tag_ids": sorted(x.tag_id for x in conn.execute(sa.select(conversation_tags).where(
                    conversation_tags.c.user_id == user_id, conversation_tags.c.conversation_id == conversation_id)))}

    def bulk(self, user_id: str, body: dict, *, readable) -> dict:
        items = body.get("items") or []
        action = body.get("action")
        if action not in ("archive", "unarchive", "pin", "unpin", "move", "tag", "untag"):
            raise Err(422, "validation_failed", "action is archive, unarchive, pin, unpin, move, tag or untag.")
        if not 1 <= len(items) <= self.settings()["bulk_limit"]:
            raise Err(422, "validation_failed", f"Choose 1 to {self.settings()['bulk_limit']} conversations.")
        for it in items:  # an unreadable id is just "not found", and nothing changes
            try:
                readable(it["id"])
            except Exception:  # noqa: BLE001
                raise Err(404, "resource_not_found", "One of those conversations was not found.")
        args = body.get("arguments") or {}
        out = []
        with self.engine.begin() as conn:
            for it in items:
                patch: dict = {"expected_revision": it.get("expected_revision", 0)}
                if action in ("archive", "unarchive"):
                    patch["archived"] = action == "archive"
                elif action in ("pin", "unpin"):
                    patch["pinned_position"] = args.get("position", 0) if action == "pin" else None
                elif action == "move":
                    patch["folder_id"] = args.get("folder_id")
                else:
                    cur = {x.tag_id for x in conn.execute(sa.select(conversation_tags).where(
                        conversation_tags.c.user_id == user_id, conversation_tags.c.conversation_id == it["id"]))}
                    tag = args.get("tag_id")
                    patch["tag_ids"] = sorted(cur | {tag}) if action == "tag" else sorted(cur - {tag})
                out.append(self._organize(conn, user_id, it["id"], patch))
        return {"affected": len(out), "items": out}

    def add_bookmark(self, user_id: str, body: dict, *, readable) -> dict:
        cid = str(body.get("conversation_id") or "")
        readable(cid)
        label, _ = normalize_name(body.get("label") or "Bookmark", "label")
        with self.engine.begin() as conn:
            if body.get("message_id"):
                m = conn.execute(sa.select(messages.c.conversation_id).where(messages.c.id == body["message_id"])).first()
                if m is None or m.conversation_id != cid:
                    raise Err(404, "not_found", "That message is not in this conversation.")
            count = conn.execute(sa.select(sa.func.count()).select_from(bookmarks).where(
                bookmarks.c.user_id == user_id)).scalar() or 0
            if count >= self.settings()["max_bookmarks"]:
                raise Err(429, "limit_reached", "You have the most bookmarks allowed.")
            bid = str(uuid.uuid4())
            t = _now()
            conn.execute(bookmarks.insert().values(id=bid, hub_id=self.hub, user_id=user_id, conversation_id=cid,
                                                   message_id=body.get("message_id"), branch_id=body.get("branch_id"),
                                                   label=label, position=count + 1, revision=1, created_at=t,
                                                   updated_at=t))
            self._bump(conn, user_id)
            return self._bookmark_json(conn.execute(sa.select(bookmarks).where(bookmarks.c.id == bid)).first())

    def delete_bookmark(self, user_id: str, bookmark_id: str, expected_revision: int) -> None:
        with self.engine.begin() as conn:
            r = conn.execute(sa.select(bookmarks).where(bookmarks.c.id == bookmark_id)).first()
            if r is None or r.user_id != user_id:
                raise Err(404, "not_found", "That bookmark was not found.")
            if r.revision != expected_revision:
                raise Err(409, "revision_conflict", "That bookmark changed.", current_revision=r.revision)
            conn.execute(bookmarks.delete().where(bookmarks.c.id == bookmark_id))
            self._bump(conn, user_id)

    # -- manifest ---------------------------------------------------------------------------
    def apply_manifest(self, user_id: str, manifest: dict, *, dry_run: bool, digest: str | None, readable) -> dict:
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("scope") != "self":
            raise Err(422, "validation_failed", "A manifest has version: 1 and scope: self.")
        unknown = set(manifest) - {"version", "scope", "expected_revision", "folders", "tags", "placements",
                                   "bookmarks", "delete"}
        if unknown:
            raise Err(422, "validation_failed", f"Unknown manifest key {sorted(unknown)[0]!r}.")
        current = self.export(user_id)
        if manifest.get("expected_revision", current["revision"]) != current["revision"]:
            raise Err(409, "revision_conflict", "Your organisation changed. Export it again.",
                      current_revision=current["revision"])
        have_f = {f["id"]: f for f in current["folders"]}
        have_t = {t["id"]: t for t in current["tags"]}
        plan = {"create_folders": [f for f in manifest.get("folders") or [] if f.get("id") not in have_f],
                "update_folders": [f for f in manifest.get("folders") or [] if f.get("id") in have_f],
                "create_tags": [t for t in manifest.get("tags") or [] if t.get("id") not in have_t],
                "placements": manifest.get("placements") or [], "bookmarks": manifest.get("bookmarks") or [],
                "delete": manifest.get("delete") or []}
        plan_digest = canonical.digest({"plan": plan, "base": current["revision"]})
        if dry_run:
            return {"digest": plan_digest, "diff": {k: len(v) for k, v in plan.items()}, "plan": plan}
        if digest != plan_digest:
            raise Err(409, "revision_conflict", "Apply exactly the dry run you reviewed.")
        for p in plan["placements"]:
            readable(p["conversation_id"])
        # Folders in parent order so a child can name a parent created here.
        for f in sorted(plan["create_folders"], key=lambda f: 0 if f.get("parent_id") in (None, *have_f) else 1):
            self.create_folder(user_id, f)
        for f in plan["update_folders"]:
            cur = self.export(user_id)
            rev = next(x["revision"] for x in cur["folders"] if x["id"] == f["id"])
            self.update_folder(user_id, f["id"], {**{k: v for k, v in f.items() if k != "id"}, "expected_revision": rev})
        for t in plan["create_tags"]:
            self.create_tag(user_id, t)
        with self.engine.begin() as conn:
            for p in plan["placements"]:
                cur = conn.execute(sa.select(placements.c.revision).where(
                    placements.c.user_id == user_id, placements.c.conversation_id == p["conversation_id"])).scalar() or 0
                self._organize(conn, user_id, p["conversation_id"], {**p, "expected_revision": cur})
        for b in plan["bookmarks"]:
            self.add_bookmark(user_id, b, readable=readable)
        for d in plan["delete"]:
            kind = d.get("kind")
            if kind == "folder":
                self.delete_folder(user_id, d["id"], {"expected_revision": d.get("expected_revision"),
                                                      "disposition": d.get("disposition", "move_to_parent")})
            elif kind == "tag":
                self.delete_tag(user_id, d["id"], d.get("expected_revision"))
            elif kind == "bookmark":
                self.delete_bookmark(user_id, d["id"], d.get("expected_revision"))
            else:
                raise Err(422, "validation_failed", "delete kind is folder, tag or bookmark.")
        return {"applied": True, "digest": plan_digest, "revision": self.export(user_id)["revision"]}
