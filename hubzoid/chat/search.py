"""Full-text search over conversations and messages, filtered by access first.

``hz_chat_search_documents`` holds one row per searchable part (a
conversation's title, each visible message's text and attachment names). The
database's own engine matches it: an FTS5 table on SQLite, ``to_tsvector`` on
PostgreSQL. No external search service is needed.

Access is applied in the same SQL as the match, before ranking, limits and
snippets: only conversations the caller owns or is a member of, never deleted
ones. Results carry no total count. Reasoning, tool arguments and results are
never indexed. Indexing follows the conversation's events (``reindex``); a
deletion is hidden at once because the access filter reads the live tables.

Query text is literal: words are ANDed, a balanced quoted part is a phrase,
the final unquoted word of two or more characters also matches as a prefix,
and punctuation has no operator meaning.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import unicodedata

import sqlalchemy as sa

from ..control import canonical
from . import graph as g
from .organization import conversation_tags, placements
from .store import conversations, messages

metadata = sa.MetaData()
Err = g.GraphError

documents = sa.Table(
    "hz_chat_search_documents", metadata,
    sa.Column("id", sa.String(160), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("message_id", sa.String(64)),
    sa.Column("branch_id", sa.String(64)),
    sa.Column("title", sa.Text),
    sa.Column("body_text", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

TABLES = (documents,)
_TOKEN = re.compile(r"\w+", re.UNICODE)


def parse_query(q: str, max_chars: int) -> tuple[list[str], list[str], str | None]:
    """(words, phrases, prefix word). Raises on unbalanced quotes."""
    q = unicodedata.normalize("NFC", q or "").strip()
    if len(q) > max_chars:
        raise Err(400, "invalid_request", f"Search text is at most {max_chars} characters.")
    if q.count('"') % 2:
        raise Err(400, "invalid_request", "Close the quote in your search.", fields={"q": "unbalanced quote"})
    phrases = [" ".join(_TOKEN.findall(p)) for p in re.findall(r'"([^"]*)"', q)]
    rest = re.sub(r'"[^"]*"', " ", q)
    words = _TOKEN.findall(rest)
    prefix = words[-1] if words and len(words[-1]) >= 2 and not rest.rstrip().endswith(" ") else None
    return words, [p for p in phrases if p], prefix


def _fts_expression(words: list[str], phrases: list[str], prefix: str | None) -> str:
    parts = []
    for w in words:
        quoted = '"' + w.replace('"', "") + '"'
        parts.append(quoted + ("*" if w == prefix else ""))
    parts += ['"' + p.replace('"', "") + '"' for p in phrases]
    return " AND ".join(parts)


def _body(content: list) -> str:
    texts, names = [], []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            texts.append(str(p.get("text") or ""))
        elif p.get("type") in ("file", "image"):
            names.append(str(p.get("name") or p.get("file_id") or ""))
    return "\n".join(texts + names)



_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"(^|\s)#{1,6}\s+|[*`~>|]+|(?<!\w)__?|__?(?!\w)")


def _plain(text: str) -> str:
    """Message text without markdown marks, on one line, for a snippet."""
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_MARKS.sub(lambda m: m.group(1) or " ", text)
    return " ".join(text.split())

class SearchService:
    def __init__(self, engine, hub: str, hub_dir):
        self.engine = engine
        self.hub = hub
        self.hub_dir = hub_dir
        self.dialect = engine.dialect.name
        self._ready = False
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None

    def settings(self) -> dict:
        from .config import service_for

        return service_for(self.hub_dir).active().document["search"]

    def ensure(self) -> None:
        if self._ready:
            return
        if self.dialect == "sqlite":
            with self.engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS hz_chat_search_fts USING fts5("
                    "doc_id UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 2')")
        self._ready = True

    # -- indexing ----------------------------------------------------------------------
    def reindex(self, conversation_id: str) -> None:
        """Bring one conversation's documents up to date (idempotent)."""
        self.ensure()
        limit = self.settings()["index_body_max_bytes"]
        with self.engine.begin() as conn:
            conv = conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()
            old = [r.id for r in conn.execute(sa.select(documents.c.id).where(
                documents.c.conversation_id == conversation_id))]
            conn.execute(documents.delete().where(documents.c.conversation_id == conversation_id))
            if self.dialect == "sqlite" and old:
                for i in range(0, len(old), 500):
                    chunk = old[i:i + 500]
                    conn.exec_driver_sql("DELETE FROM hz_chat_search_fts WHERE doc_id IN (%s)"
                                         % ",".join("?" * len(chunk)), tuple(chunk))
            if conv is None or conv.deleted_at is not None or conv.hub != self.hub:
                return
            rows = [(f"c:{conv.id}", "conversation", None, None, conv.title or "", conv.title or "")]
            for m in conn.execute(sa.select(messages).where(messages.c.conversation_id == conversation_id,
                                                            messages.c.deleted_at.is_(None),
                                                            messages.c.status != "running")):
                body = _body(json.loads(m.content or "[]")).encode("utf-8")[:limit].decode("utf-8", "ignore")
                if body.strip():
                    rows.append((f"m:{m.id}", "message", m.id, m.branch_id, conv.title or "", body))
            t = time.time()
            for doc_id, kind, mid, bid, title, body in rows:
                conn.execute(documents.insert().values(id=doc_id, hub_id=self.hub, kind=kind,
                                                       conversation_id=conversation_id, message_id=mid, branch_id=bid,
                                                       title=title, body_text=body, updated_at=t))
                if self.dialect == "sqlite":
                    conn.exec_driver_sql("INSERT INTO hz_chat_search_fts(doc_id, title, body) VALUES (?, ?, ?)",
                                         (doc_id, title, body))

    def schedule(self, conversation_id: str, delay: float = 0.5) -> None:
        """Reindex soon (coalesced), off the caller's thread."""
        with self._lock:
            self._pending.add(conversation_id)
            if self._timer is None:
                self._timer = threading.Timer(delay, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            ids, self._pending, self._timer = list(self._pending), set(), None
        for cid in ids:
            try:
                self.reindex(cid)
            except Exception:  # noqa: BLE001 - search lags; access still filters live
                import logging

                logging.getLogger("hubzoid.chat.search").warning("search: reindex of %s failed", cid, exc_info=True)

    def rebuild(self) -> dict:
        started = time.time()
        with self.engine.connect() as conn:
            ids = [r.id for r in conn.execute(sa.select(conversations.c.id).where(conversations.c.hub == self.hub))]
        for cid in ids:
            self.reindex(cid)
        return {"state": "complete", "conversations": len(ids), "seconds": round(time.time() - started, 3)}

    def status(self) -> dict:
        self.ensure()
        with self.engine.connect() as conn:
            n = conn.execute(sa.select(sa.func.count()).select_from(documents).where(
                documents.c.hub_id == self.hub)).scalar() or 0
        return {"state": "ready", "documents": n, "engine": "fts5" if self.dialect == "sqlite" else "postgres",
                "pending": len(self._pending)}

    # -- searching -----------------------------------------------------------------------
    def search(self, actor, q: str, *, types: list[str] | None = None, limit: int | None = None,
               cursor: str | None = None, archived: str = "exclude", folder_id: str | None = None,
               tag_ids: list[str] | None = None, pinned: bool | None = None) -> dict:
        cfg = self.settings()
        if not cfg["enabled"]:
            raise Err(403, "feature_disabled", "Search is turned off for this deployment.")
        self.ensure()
        limit = max(1, min(int(limit or cfg["page_size"]), cfg["max_page_size"]))
        types = types or cfg["default_types"]
        words, phrases, prefix = parse_query(q, cfg["query_max_chars"])
        offset = 0
        qhash = canonical.digest([q, types, archived, folder_id, tag_ids, pinned])[7:23]
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if raw["q"] != qhash or raw["a"] != actor.id:
                    raise ValueError
                offset = int(raw["o"])
            except (ValueError, KeyError, TypeError):
                raise Err(409, "cursor_stale", "The results changed. Search again.")
        # Access first: conversations this person may read, never deleted ones.
        member = sa.select(g.members.c.conversation_id).where(sa.or_(
            sa.and_(g.members.c.principal_type == "user", g.members.c.principal_id.in_([actor.id, actor.email])),
            sa.and_(g.members.c.principal_type == "group", g.members.c.principal_id.in_(list(actor.groups) or [""]))))
        visible = sa.select(conversations.c.id).where(
            conversations.c.hub == self.hub, conversations.c.deleted_at.is_(None),
            # A temporary conversation past its expiry is gone, cleaned up or not.
            sa.or_(conversations.c.temporary == 0, conversations.c.expires_at.is_(None),
                   conversations.c.expires_at > time.time()),
            sa.or_(conversations.c.owner_id == actor.id, conversations.c.id.in_(member)))
        place = sa.select(placements.c.conversation_id).where(placements.c.user_id == actor.id)
        if archived == "exclude":
            visible = visible.where(conversations.c.archived == 0)
        elif archived == "only":
            visible = visible.where(conversations.c.archived == 1)
        if folder_id:
            visible = visible.where(conversations.c.id.in_(place.where(placements.c.folder_id == folder_id)))
        if pinned is not None:
            visible = visible.where(conversations.c.id.in_(place.where(placements.c.pinned_rank.is_not(None))) if pinned
                                    else ~conversations.c.id.in_(place.where(placements.c.pinned_rank.is_not(None))))
        for tid in tag_ids or []:
            visible = visible.where(conversations.c.id.in_(sa.select(conversation_tags.c.conversation_id).where(
                conversation_tags.c.user_id == actor.id, conversation_tags.c.tag_id == tid)))
        d = documents
        base = sa.select(d.c.id, d.c.kind, d.c.conversation_id, d.c.message_id, d.c.branch_id, d.c.title,
                         d.c.body_text, d.c.updated_at).where(d.c.hub_id == self.hub, d.c.kind.in_(types),
                                                              d.c.conversation_id.in_(visible))
        with self.engine.connect() as conn:
            if not words and not phrases:
                rows = conn.execute(base.order_by(d.c.updated_at.desc(), d.c.id).offset(offset).limit(limit + 1)).all()
                ranked = [(r, 0) for r in rows]
            elif self.dialect == "sqlite":
                expr = _fts_expression(words, phrases, prefix)
                hits = sa.text("SELECT doc_id, bm25(hz_chat_search_fts, 0.0, 10.0, 1.0) AS score FROM hz_chat_search_fts "
                               "WHERE hz_chat_search_fts MATCH :q").bindparams(q=expr).columns(
                    sa.column("doc_id"), sa.column("score")).subquery()
                stmt = base.add_columns(hits.c.score).join(hits, hits.c.doc_id == d.c.id).order_by(
                    hits.c.score, d.c.updated_at.desc(), d.c.id).offset(offset).limit(limit + 1)
                try:
                    ranked = [(r, r.score) for r in conn.execute(stmt).all()]
                except sa.exc.OperationalError:
                    raise Err(400, "invalid_request", "That search could not be read. Try plain words.")
            else:
                text = " & ".join([w + (":*" if w == prefix else "") for w in words] +
                                  ["(" + " <-> ".join(p.split()) + ")" for p in phrases])
                tsq = sa.func.to_tsquery("simple", text)
                vec = sa.func.to_tsvector("simple", sa.func.coalesce(d.c.title, "") + " " + d.c.body_text)
                stmt = base.where(vec.op("@@")(tsq)).order_by(d.c.updated_at.desc(), d.c.id).offset(offset).limit(limit + 1)
                ranked = [(r, 0) for r in conn.execute(stmt).all()]
        more = len(ranked) > limit
        terms = [w.casefold() for w in words] + [p.casefold() for p in phrases]
        items = [self._result(r, terms) for r, _ in ranked[:limit]]
        nxt = None
        if more:
            nxt = base64.urlsafe_b64encode(json.dumps({"q": qhash, "a": actor.id, "o": offset + limit}).encode()
                                           ).decode().rstrip("=")
        return {"items": items, "next_cursor": nxt, "index_status": "ready"}

    @staticmethod
    def _result(r, terms: list[str]) -> dict:
        body = _plain(r.body_text or "")
        low = body.casefold()
        at = min([i for i in (low.find(t) for t in terms if t) if i >= 0] or [0])
        start = max(0, at - 60)
        if start:  # begin at a word, not inside one
            space = body.rfind(" ", 0, start + 1)
            start = space + 1 if space >= 0 and at - space < 120 else start
        end = min(len(body), start + 240)
        if end < len(body):
            space = body.rfind(" ", start, end)
            end = space if space > at else end
        snippet = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
        highlights = []
        for t in terms:
            i = snippet.casefold().find(t)
            if t and i >= 0:
                highlights.append([i, i + len(t)])
        return {"kind": r.kind, "resource_id": r.message_id or r.conversation_id, "title": r.title or "",
                "snippet": snippet, "highlights": highlights, "updated_at": r.updated_at,
                "open_target": {"conversation_id": r.conversation_id, "branch_id": r.branch_id,
                                "message_id": r.message_id}}

