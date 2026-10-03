"""Conversations, messages and shares on the operational store.

SQLAlchemy Core over the shared operational engine (``db.operational_engine``),
so it runs the same on SQLite and PostgreSQL and every bridge of a gateway sees
the same history. The schema is migration ``op_0010`` (``hz_conversations``,
``hz_messages``, ``hz_shares``).

A conversation belongs to one person (``owner_id``) and one agent of one hub.
Messages form a tree through ``parent_id``: an edit is a new user message beside
the old one, a regeneration a new assistant message beside the old reply.
``head_id`` is the message the person last looked at; the active branch is the
path from the root to it. Ids are client-generated (see ``valid_message_id``)
and globally unique, so an id already used in another conversation is refused.

A conversation's files (uploads, the agent's artifacts) live in the hub folder
under ``.hubzoid/chats/<key>/``, ``chat_key`` below, which is also the chat
scope of its runs. That folder tree is shared with every other surface (Open
WebUI chats of Open WebUI mode, Slack, Telegram, WhatsApp), so a conversation
started here uses ``web-<id>``: an id the browser chose never names another
surface's chat. A conversation imported from Open WebUI keeps its id, the
folder ``hubzoid migrate openwebui`` copied its files to. Web conversation ids
are unique ignoring case (index ``hz_conversations_web_id``), so two of them
never share a folder on a case-insensitive file system.

Everything here is synchronous (callers in the event loop use a thread).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .. import memory as memlib

log = logging.getLogger(__name__)

metadata = sa.MetaData()

conversations = sa.Table(
    "hz_conversations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("owner_email", sa.String(320), nullable=False),
    sa.Column("hub", sa.String(255), nullable=False),
    sa.Column("agent", sa.String(255), nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("title_source", sa.String(16), nullable=False),
    sa.Column("archived", sa.Integer, nullable=False),
    sa.Column("head_id", sa.String(64)),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

messages = sa.Table(
    "hz_messages", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False),
    sa.Column("parent_id", sa.String(64)),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("text", sa.Text),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("model", sa.String(255)),
    sa.Column("usage", sa.Text),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

shares = sa.Table(
    "hz_shares", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("conversation_id", sa.String(64), nullable=False),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("agent", sa.String(255)),
    sa.Column("snapshot", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

# Client-generated ids (contract 6.2).
MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# A conversation id also names the per-chat folder, ``web-<id>`` (``chat_key``).
# Download links pass that name through ``memory.sanitize_chat_id``, which trims
# leading and trailing ``-`` and ``_`` and keeps 64 characters, so the id starts
# and ends with a letter or digit and has at most 60 characters: the folder,
# download links and tools then agree on one name.
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{6,58}[A-Za-z0-9]$")

# ``source`` of a conversation imported by ``hubzoid migrate openwebui``.
MIGRATED = "migrated"
# The per-chat folder of a conversation started in the web app is this plus its id.
WEB_PREFIX = "web-"

STATUSES = ("complete", "running", "cancelled", "error")
DEFAULT_PAGE = 50
MAX_PAGE = 200


class StoreError(Exception):
    """A request the store refuses. ``code`` is the API error code."""

    code = "invalid"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class IdConflict(StoreError):
    code = "id_conflict"


class InvalidCursor(StoreError):
    code = "invalid_cursor"


def valid_message_id(value: Any) -> bool:
    return isinstance(value, str) and bool(MESSAGE_ID.fullmatch(value))


def valid_conversation_id(value: Any) -> bool:
    return isinstance(value, str) and bool(CONVERSATION_ID.fullmatch(value))


def chat_key(conv: dict) -> str:
    """The conversation's per-chat folder under ``.hubzoid/chats/`` and the
    chat scope of its runs: ``web-<id>`` for a conversation started in the web
    app, the Open WebUI id for an imported one (the folder the import copied
    its files to, named as ``hubzoid migrate openwebui`` names it)."""
    conv_id = str(conv["id"])
    if conv.get("source") == MIGRATED:
        return memlib.sanitize_chat_id(conv_id) or conv_id
    return WEB_PREFIX + conv_id


def new_id(prefix: str) -> str:
    """A fresh id that satisfies both id rules (``c_``/``m_`` plus 22 letters and digits)."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(22))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def encode_cursor(updated_at: float, conv_id: str) -> str:
    raw = _dumps([updated_at, conv_id]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        updated_at, conv_id = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(conv_id, str) or not isinstance(updated_at, (int, float)):
            raise ValueError("cursor shape")
        return float(updated_at), conv_id
    except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
        raise InvalidCursor("That page link is not valid. Reload the list.") from exc


def _conversation(row) -> dict:
    d = dict(row._mapping)
    d["archived"] = bool(d.get("archived"))
    return d


def _message(row) -> dict:
    d = dict(row._mapping)
    d["content"] = _loads(d.get("content"), [])
    d["usage"] = _loads(d.get("usage"), None)
    return d


def remove_chat_files(hub_dir: Path, key: str) -> None:
    """Delete the conversation's files folder (its ``store.chat_key``), only
    ever inside the hub's chats folder."""
    base = (Path(hub_dir) / memlib.CHATS_DIRNAME).resolve()
    target = memlib.chat_root(Path(hub_dir), key)
    if not target.exists():
        return
    resolved = target.resolve()
    if (not base.is_relative_to(Path(hub_dir).resolve())
            or resolved.parent != base or target.is_symlink()):
        log.warning("chat: refused to delete %s (outside %s)", target, base)
        return
    shutil.rmtree(resolved, ignore_errors=True)


def delete_owner_in(conn, owner_id: str) -> None:
    """Remove native chat data in the account deletion transaction."""
    owned = sa.select(conversations.c.id).where(conversations.c.owner_id == owner_id)
    conn.execute(messages.delete().where(messages.c.conversation_id.in_(owned)))
    conn.execute(shares.delete().where(sa.or_(shares.c.owner_id == owner_id,
                                             shares.c.conversation_id.in_(owned))))
    conn.execute(conversations.delete().where(conversations.c.owner_id == owner_id))


class ConversationStore:
    """History for the web app. One instance per operational engine."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- conversations ---------------------------------------------------------
    def create_conversation(self, *, conv_id: str, owner_id: str, owner_email: str, hub: str,
                            agent: str, title: str | None = None, title_source: str = "pending",
                            source: str = "web", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        row = dict(id=conv_id, owner_id=owner_id, owner_email=owner_email, hub=hub, agent=agent,
                   title=title, title_source=title_source, archived=0, head_id=None, source=source,
                   created_at=now, updated_at=now)
        try:
            with self.engine.begin() as conn:
                conn.execute(conversations.insert().values(**row))
        except IntegrityError as exc:
            raise IdConflict("That conversation id is already in use.") from exc
        return {**row, "archived": False}

    def get_conversation(self, conv_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(conversations.select().where(conversations.c.id == conv_id)).first()
        return _conversation(row) if row else None

    def conversation_for_chat_key(self, key: str) -> dict | None:
        """The conversation whose per-chat folder is ``key`` (the reverse of
        ``chat_key``, exact, case included), or None."""
        ids = [key[len(WEB_PREFIX):]] if key.startswith(WEB_PREFIX) else []
        ids.append(key)
        for conv_id in ids:
            conv = self.get_conversation(conv_id) if conv_id else None
            if conv is not None and chat_key(conv) == key:
                return conv
        return None

    def owned_conversation(self, conv_id: str, owner_id: str) -> dict | None:
        """The conversation when ``owner_id`` owns it, else None (never tells
        whether someone else's exists)."""
        conv = self.get_conversation(conv_id)
        return conv if conv is not None and conv["owner_id"] == owner_id else None

    def list_conversations(self, owner_id: str, *, archived: bool = False, q: str | None = None,
                           limit: int = DEFAULT_PAGE, cursor: str | None = None,
                           ) -> tuple[list[dict], str | None]:
        """Newest activity first (``updated_at``, then id). Returns the page and
        the cursor of the next page (None on the last page)."""
        limit = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
        c = conversations.c
        stmt = sa.select(conversations).where(c.owner_id == owner_id,
                                              c.archived == (1 if archived else 0))
        q = (q or "").strip()
        if q:
            pattern = _like_pattern(q)
            in_text = sa.exists(sa.select(sa.literal(1)).where(
                messages.c.conversation_id == c.id,
                messages.c.text.ilike(pattern, escape="\\")))
            stmt = stmt.where(sa.or_(c.title.ilike(pattern, escape="\\"), in_text))
        if cursor:
            after_ts, after_id = decode_cursor(cursor)
            stmt = stmt.where(sa.or_(c.updated_at < after_ts,
                                     sa.and_(c.updated_at == after_ts, c.id < after_id)))
        stmt = stmt.order_by(c.updated_at.desc(), c.id.desc()).limit(limit + 1)
        with self.engine.connect() as conn:
            rows = [_conversation(r) for r in conn.execute(stmt)]
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1]["updated_at"], rows[-1]["id"]) if more and rows else None
        return rows, next_cursor

    def update_conversation(self, conv_id: str, **fields) -> dict | None:
        """Set any of title, title_source, archived, head_id, updated_at."""
        allowed = {"title", "title_source", "archived", "head_id", "updated_at"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if "archived" in values:
            values["archived"] = 1 if values["archived"] else 0
        if values:
            with self.engine.begin() as conn:
                conn.execute(conversations.update().where(conversations.c.id == conv_id)
                             .values(**values))
        return self.get_conversation(conv_id)

    def touch(self, conv_id: str, *, head_id: str | None = None, now: float | None = None) -> None:
        values: dict = {"updated_at": time.time() if now is None else now}
        if head_id is not None:
            values["head_id"] = head_id
        with self.engine.begin() as conn:
            conn.execute(conversations.update().where(conversations.c.id == conv_id).values(**values))

    def set_title(self, conv_id: str, title: str, source: str, *,
                  only_if_source: tuple[str, ...] | None = None) -> bool:
        """Set the title; with ``only_if_source``, only while the current source
        is one of those (a person's own title is never overwritten)."""
        stmt = conversations.update().where(conversations.c.id == conv_id)
        if only_if_source is not None:
            stmt = stmt.where(conversations.c.title_source.in_(only_if_source))
        with self.engine.begin() as conn:
            return conn.execute(stmt.values(title=title, title_source=source)).rowcount > 0

    def set_title_source(self, conv_id: str, source: str, *,
                         only_if_source: tuple[str, ...] | None = None) -> bool:
        stmt = conversations.update().where(conversations.c.id == conv_id)
        if only_if_source is not None:
            stmt = stmt.where(conversations.c.title_source.in_(only_if_source))
        with self.engine.begin() as conn:
            return conn.execute(stmt.values(title_source=source)).rowcount > 0

    def delete_conversation(self, conv_id: str) -> bool:
        with self.engine.begin() as conn:
            conn.execute(messages.delete().where(messages.c.conversation_id == conv_id))
            conn.execute(shares.delete().where(shares.c.conversation_id == conv_id))
            return conn.execute(conversations.delete().where(conversations.c.id == conv_id)).rowcount > 0

    # -- messages --------------------------------------------------------------
    def insert_message(self, *, message_id: str, conversation_id: str, parent_id: str | None,
                       role: str, content: list, text: str | None = None,
                       status: str = "complete", model: str | None = None,
                       now: float | None = None) -> dict:
        now = time.time() if now is None else now
        row = dict(id=message_id, conversation_id=conversation_id, parent_id=parent_id, role=role,
                   content=_dumps(content), text=text, status=status, error=None, model=model,
                   usage=None, created_at=now, updated_at=now)
        try:
            with self.engine.begin() as conn:
                conn.execute(messages.insert().values(**row))
        except IntegrityError as exc:
            raise IdConflict("That message id is already in use.") from exc
        return {**row, "content": content, "usage": None}

    def get_message(self, message_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(messages.select().where(messages.c.id == message_id)).first()
        return _message(row) if row else None

    def update_message(self, message_id: str, **fields) -> None:
        """Set any of content (list), text, status, error, model, usage (dict)."""
        values: dict = {}
        for key, value in fields.items():
            if key == "content":
                values["content"] = _dumps(value)
            elif key == "usage":
                values["usage"] = _dumps(value) if value is not None else None
            elif key in ("text", "status", "error", "model"):
                values[key] = value
        if not values:
            return
        values["updated_at"] = time.time()
        with self.engine.begin() as conn:
            conn.execute(messages.update().where(messages.c.id == message_id).values(**values))

    def list_messages(self, conv_id: str) -> list[dict]:
        stmt = (messages.select().where(messages.c.conversation_id == conv_id)
                .order_by(messages.c.created_at, messages.c.id))
        with self.engine.connect() as conn:
            return [_message(r) for r in conn.execute(stmt)]

    def branch(self, conv_id: str, message_id: str | None) -> list[dict]:
        """The messages from the root down to ``message_id`` (inclusive), or []
        when it is not a message of this conversation."""
        if not message_id:
            return []
        with self.engine.connect() as conn:
            links = dict(conn.execute(sa.select(messages.c.id, messages.c.parent_id)
                                      .where(messages.c.conversation_id == conv_id)).all())
            if message_id not in links:
                return []
            path: list[str] = []
            seen: set[str] = set()
            node: str | None = message_id
            while node is not None and node in links and node not in seen:
                seen.add(node)
                path.append(node)
                node = links[node]
            path.reverse()
            rows = {r.id: _message(r) for r in conn.execute(
                messages.select().where(messages.c.id.in_(path)))}
        return [rows[i] for i in path if i in rows]

    def latest_leaf(self, conv_id: str) -> str | None:
        """The newest message that has no reply: the default head."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(messages.c.id, messages.c.parent_id)
                                .where(messages.c.conversation_id == conv_id)
                                .order_by(messages.c.created_at.desc(), messages.c.id.desc())).all()
        parents = {parent for _, parent in rows if parent}
        return next((mid for mid, _ in rows if mid not in parents), None)

    def sweep_running(self, hub: str, *, error: str) -> int:
        """Mark this hub's replies still 'running' as failed (their run ended with
        the process that owned it). Returns how many."""
        owned = sa.select(conversations.c.id).where(conversations.c.hub == hub)
        with self.engine.begin() as conn:
            return conn.execute(
                messages.update()
                .where(messages.c.status == "running", messages.c.conversation_id.in_(owned))
                .values(status="error", error=error, updated_at=time.time())).rowcount

    # -- shares ----------------------------------------------------------------
    def share_for(self, conv_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(shares.select().where(shares.c.conversation_id == conv_id)
                               .order_by(shares.c.created_at.desc())).first()
        return dict(row._mapping) if row else None

    def get_share(self, share_id: str) -> dict | None:
        with self.engine.connect() as conn:
            # A share whose conversation was deleted (including a racing late
            # snapshot write) must never make the deleted history readable.
            row = conn.execute(shares.select().where(
                shares.c.id == share_id,
                sa.exists(sa.select(conversations.c.id).where(
                    conversations.c.id == shares.c.conversation_id,
                    conversations.c.owner_id == shares.c.owner_id)),
            )).first()
        if row is None:
            return None
        d = dict(row._mapping)
        d["snapshot"] = _loads(d.get("snapshot"), {})
        return d

    def save_share(self, *, conversation_id: str, owner_id: str, title: str | None,
                   agent: str | None, snapshot: dict) -> dict:
        """Store a snapshot for the conversation. A conversation keeps one link:
        sharing again refreshes the snapshot behind the same id."""
        now = time.time()
        with self.engine.begin() as conn:
            existing = conn.execute(sa.select(shares.c.id)
                                    .where(shares.c.conversation_id == conversation_id)).scalar()
            values = dict(owner_id=owner_id, title=title, agent=agent, snapshot=_dumps(snapshot),
                          created_at=now)
            if existing:
                conn.execute(shares.update().where(shares.c.id == existing).values(**values))
                share_id = existing
            else:
                share_id = secrets.token_urlsafe(16)
                conn.execute(shares.insert().values(id=share_id, conversation_id=conversation_id,
                                                    **values))
        return {"id": share_id, "conversation_id": conversation_id, "created_at": now, **values}

    def delete_shares(self, conv_id: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(shares.delete().where(shares.c.conversation_id == conv_id)).rowcount


_stores: dict[int, ConversationStore] = {}
_stores_lock = threading.Lock()


def for_hub(hub_dir: Path) -> ConversationStore:
    """The store on this deployment's operational engine (schema brought up to
    date once per engine)."""
    from .. import db, migrations

    engine = db.operational_engine(Path(hub_dir))
    key = id(engine)
    with _stores_lock:
        store = _stores.get(key)
        if store is None or store.engine is not engine:
            migrations.upgrade(engine, "operational")
            store = ConversationStore(engine)
            _stores[key] = store
    return store
