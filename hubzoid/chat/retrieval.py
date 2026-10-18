"""Sources, retrieval and safe web fetching (HUB-75).

A **collection** is a named set of sources a person keeps for answers: uploaded
files (HUB-74 file ids), files or folders of the hub, short text notes, and
public web pages. Its manifest (title, readers, writers, agents, sources) is
revisioned; ``hubzoid knowledge collections apply`` and the API share it.

**Syncing** extracts text with built-in extractors (text, Markdown, CSV, JSON,
HTML, PDF), cuts it into overlapping passages and indexes them for local
lexical search (SQLite FTS5 BM25; PostgreSQL full-text search). A sync builds a
new generation and publishes it in one step only when it finished, so a failed
or partial sync never removes what was there; a removed source leaves results
at once.

A **query** first works out which collections this person may read (owner,
named readers, or everyone with hub use) and which agent asks, then searches
only those passages. Each result becomes a **citation**: a stored record of the
exact source version and passage, resolved again (and re-authorised) whenever
it is opened. Text that merely looks like a citation in an answer resolves to
nothing.

**Public fetching** takes only https URLs on the standard port, refuses user
names in URLs, checks every address the name resolves to (and every redirect
hop) is public, pins the checked address for the connection, never sends
cookies or credentials, and stops at the byte and time limits.
"""
from __future__ import annotations

import html as htmllib
import io
import json
import logging
import re
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..control import canonical
from . import graph as g

log = logging.getLogger("hubzoid.chat.retrieval")
metadata = sa.MetaData()
Err = g.GraphError
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CHARS_PER_TOKEN = 4
STOP = frozenset("a an and are as at be by can do does for from has have how i in is it its may must of on or our "
                 "should that the their there this to was we what when where which who why will with you your".split())
EXCERPT = 600


# -- settings ------------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chunking(_Strict):
    tokens: int = Field(512, ge=64, le=4096)
    overlap: int = Field(64, ge=0, le=1024)


class Search(_Strict):
    mode: Literal["lexical"] = "lexical"
    top_k: int = Field(8, ge=1, le=50)
    candidate_k: int = Field(40, ge=1, le=200)


class Limits(_Strict):
    query_chars: int = Field(4000, ge=16, le=4000)
    whole_document_tokens: int | None = Field(None, ge=1)
    collections_per_query: int = Field(20, ge=1, le=100)
    max_source_bytes: int = Field(26_214_400, ge=1)


class WebLimits(_Strict):
    redirects: int = Field(10, ge=0, le=20)
    bytes: int = Field(5_242_880, ge=1024, le=52_428_800)
    timeout_seconds: int = Field(30, ge=1, le=120)


class Web(_Strict):
    enabled: bool = False
    search: Literal["native", "off"] = "native"
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    limits: WebLimits = Field(default_factory=WebLimits)


class RetrievalSettings(_Strict):
    enabled: bool = True
    default_mode: Literal["retrieve", "whole"] = "retrieve"
    extractor: Literal["builtin"] = "builtin"
    chunking: Chunking = Field(default_factory=Chunking)
    search: Search = Field(default_factory=Search)
    limits: Limits = Field(default_factory=Limits)
    web: Web = Field(default_factory=Web)


def register() -> None:
    from .config import namespaces, register_namespace

    ns = namespaces().get("retrieval")
    if ns is None or not ns.installed:
        register_namespace("retrieval", RetrievalSettings, owner="HUB-75")


register()


class SourceSpec(_Strict):
    id: str
    kind: Literal["upload", "file", "directory", "text", "url"]
    file_id: str | None = None
    path: str | None = None
    recursive: bool = True
    text: str | None = None
    title: str | None = None
    url: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not SLUG.match(v):
            raise ValueError("a source id is lowercase letters, digits and dashes")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v):
        if v is not None and (v.startswith("/") or ".." in Path(v).parts):
            raise ValueError("a hub-relative path without '..'")
        return v


class Manifest(_Strict):
    version: Literal[1] = 1
    id: str
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    readers: list[str] = Field(default_factory=list)  # emails, "*" (everyone with hub use)
    writers: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)   # empty: every agent of the hub
    sources: list[SourceSpec] = Field(default_factory=list, max_length=500)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not SLUG.match(v):
            raise ValueError("a collection id is lowercase letters, digits and dashes")
        return v

    @field_validator("sources")
    @classmethod
    def _unique(cls, v):
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids are unique within a collection")
        return v


# -- tables -------------------------------------------------------------------------------
collections = sa.Table(
    "hz_knowledge_collections", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("owner_principal_id", sa.String(64), nullable=False),
    sa.Column("title", sa.String(255), nullable=False),
    sa.Column("manifest_json", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("active_generation_id", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)

sources = sa.Table(
    "hz_knowledge_sources", metadata,
    sa.Column("source_id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("collection_id", sa.String(64), nullable=False, index=True),
    sa.Column("key", sa.String(255), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("title", sa.String(255), nullable=False),
    sa.Column("locator", sa.Text, nullable=False),
    sa.Column("source_version", sa.Integer, nullable=False),
    sa.Column("content_sha256", sa.String(64)),
    sa.Column("file_id", sa.String(64)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("observed_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
    sa.UniqueConstraint("hub_id", "collection_id", "key", name="uq_hz_knowledge_source_key"),
)

generations = sa.Table(
    "hz_knowledge_generations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("collection_id", sa.String(64), nullable=False),
    sa.Column("manifest_revision", sa.Integer, nullable=False),
    sa.Column("fingerprint", sa.String(80), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("published_at", sa.Float),
)

chunks = sa.Table(
    "hz_knowledge_chunks", metadata,
    sa.Column("chunk_id", sa.String(64), primary_key=True),
    sa.Column("generation_id", sa.String(64), nullable=False, index=True),
    sa.Column("source_id", sa.String(64), nullable=False),
    sa.Column("source_version", sa.Integer, nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("locator_json", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
)

jobs = sa.Table(
    "hz_knowledge_jobs", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("collection_id", sa.String(64)),
    sa.Column("operation", sa.String(16), nullable=False),
    sa.Column("principal_id", sa.String(64), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("counters_json", sa.Text, nullable=False),
    sa.Column("error_code", sa.String(64)),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

citations = sa.Table(
    "hz_knowledge_citations", metadata,
    sa.Column("citation_id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("collection_id", sa.String(64), nullable=False),
    sa.Column("source_id", sa.String(64), nullable=False),
    sa.Column("source_version", sa.Integer, nullable=False),
    sa.Column("chunk_id", sa.String(64), nullable=False),
    sa.Column("generation_id", sa.String(64), nullable=False),
    sa.Column("principal_id", sa.String(64), nullable=False),
    sa.Column("conversation_id", sa.String(64)),
    sa.Column("run_id", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
)

TABLES = (collections, sources, generations, chunks, jobs, citations)
FTS = "hz_knowledge_fts"


def ensure_fts(conn) -> None:
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql(f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS} USING fts5(chunk_id UNINDEXED, text, "
                             "tokenize='unicode61 remove_diacritics 2')")


def _now() -> float:
    return time.time()


# -- extraction ----------------------------------------------------------------------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def extract(name: str, mime: str, data: bytes) -> tuple[str, list[tuple[int, str]]]:
    """(title, [(page or 0, text)]) from the bytes, with built-in extractors."""
    low = name.lower()
    if mime == "application/pdf" or low.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return name, [(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]
    text = data.decode("utf-8", "replace")
    if mime == "text/html" or low.endswith((".html", ".htm")):
        p = _Text()
        p.feed(text)
        body = re.sub(r"\n\s*\n+", "\n\n", "".join(p.out))
        return (htmllib.unescape(p.title).strip() or name), [(0, body)]
    if not (mime.startswith("text/") or mime == "application/json" or low.endswith(
            (".md", ".txt", ".csv", ".json", ".yaml", ".yml"))):
        raise Err(415, "unsupported_format", f"{name} can't be read as text by the built-in extractor.")
    return name, [(0, text)]


def chunk(pages: list[tuple[int, str]], tokens: int, overlap: int) -> list[tuple[str, dict]]:
    """Passages of about ``tokens`` tokens (4 characters each) on paragraph
    boundaries, overlapping by ``overlap`` tokens, with their locations."""
    size, keep = tokens * CHARS_PER_TOKEN, overlap * CHARS_PER_TOKEN
    out: list[tuple[str, dict]] = []
    for page, text in pages:
        text = text.replace("\r\n", "\n")
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                cut = text.rfind("\n\n", start + size // 2, end)
                if cut == -1:
                    cut = text.rfind(". ", start + size // 2, end)
                end = cut + 1 if cut != -1 else end
            piece = text[start:end].strip()
            if piece:
                out.append((piece, {"page": page or None, "start": start, "end": end}))
            if end >= len(text):
                break
            start = max(end - keep, start + 1)
    return out


# -- safe public fetching ----------------------------------------------------------------------
def safe_fetch(url: str, limits: dict, *, allowed: list[str] | None = None,
               blocked: list[str] | None = None) -> tuple[str, str, bytes]:
    """(final url, type, bytes) of a public https page; every hop checked."""
    import httpx
    from urllib.parse import urljoin, urlparse

    from .feedback import check_webhook_url, pinned_request

    hops = 0
    while True:
        u = urlparse(url)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443):
            raise Err(422, "unsafe_url", "Only public https:// addresses on the standard port can be fetched.")
        host = u.hostname.lower()
        if any(host == d or host.endswith("." + d) for d in (blocked or [])):
            raise Err(422, "unsafe_url", f"{host} is blocked for this deployment.")
        if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
            raise Err(422, "unsafe_url", f"{host} is not on this deployment's list of allowed sites.")
        try:
            address = check_webhook_url(url)
        except Err:
            raise Err(422, "unsafe_url", "That address isn't public, or doesn't resolve.")
        target, hdr, ext = pinned_request(url, address)
        try:
            with httpx.Client(timeout=limits["timeout_seconds"], follow_redirects=False) as client:
                with client.stream("GET", target, extensions=ext, headers={
                        **hdr, "user-agent": "Hubzoid-fetch/2 (+https://github.com/hubzoid/hubzoid)",
                        "accept": "text/html,text/plain,application/pdf,application/json;q=0.9,*/*;q=0.1"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        hops += 1
                        if hops > limits["redirects"]:
                            raise Err(422, "unsafe_url", "Too many redirects.")
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    if r.status_code >= 400:
                        raise Err(502, "provider_failed", f"The site answered {r.status_code}.")
                    body = bytearray()
                    for part in r.iter_bytes():
                        body += part
                        if len(body) > limits["bytes"]:
                            raise Err(413, "context_too_large", "That page is larger than the fetch limit.")
                    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    return url, mime or "text/html", bytes(body)
        except httpx.TimeoutException:
            raise Err(504, "deadline_exceeded", "The site took too long to answer.")
        except httpx.HTTPError as exc:
            raise Err(502, "provider_failed", f"The page could not be fetched ({type(exc).__name__}).")


# -- service ---------------------------------------------------------------------------------
class RetrievalService:
    def __init__(self, hub_dir: Path, engine, hub: str, files=None):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub
        self.files = files  # FileService: uploaded sources
        self.fetch = safe_fetch
        with self.engine.begin() as conn:
            ensure_fts(conn)

    def settings(self) -> dict:
        from .config import service_for

        try:
            return service_for(self.hub_dir).active().document["retrieval"]
        except Exception:  # noqa: BLE001
            return RetrievalSettings().model_dump()

    # -- access ----------------------------------------------------------------------------------
    @staticmethod
    def _may(row, actor, write: bool = False) -> bool:
        if row is None or row.deleted_at is not None:
            return False
        if row.owner_principal_id == actor.id:
            return True
        m = json.loads(row.manifest_json)
        people = m.get("writers" if write else "readers") or []
        if write:
            return actor.email in people
        return "*" in people or actor.email in people or actor.email in (m.get("writers") or [])

    def _get(self, conn, cid: str):
        return conn.execute(sa.select(collections).where(collections.c.hub_id == self.hub,
                                                         collections.c.id == cid)).first()

    def _collection_json(self, conn, row, actor) -> dict:
        m = json.loads(row.manifest_json)
        srcs = conn.execute(sa.select(sources).where(sources.c.hub_id == self.hub, sources.c.collection_id == row.id,
                                                     sources.c.deleted_at.is_(None))).all()
        job = conn.execute(sa.select(jobs).where(jobs.c.hub_id == self.hub, jobs.c.collection_id == row.id)
                           .order_by(jobs.c.created_at.desc()).limit(1)).first()
        return {"id": row.id, "title": row.title, "description": m.get("description", ""), "revision": row.revision,
                "manifest": m, "owned": row.owner_principal_id == actor.id, "can_write": self._may(row, actor, True),
                "ready": row.active_generation_id is not None, "updated_at": row.updated_at,
                "sources": [{"source_id": s.source_id, "key": s.key, "kind": s.kind, "title": s.title,
                             "state": s.state, "error": s.error, "source_version": s.source_version} for s in srcs],
                "last_job": self._job_json(job) if job else None}

    # -- collections -----------------------------------------------------------------------------
    def list(self, actor) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(collections).where(collections.c.hub_id == self.hub,
                                                             collections.c.deleted_at.is_(None))
                                .order_by(collections.c.updated_at.desc())).all()
            return [self._collection_json(conn, r, actor) for r in rows if self._may(r, actor)]

    def get(self, actor, cid: str) -> dict:
        with self.engine.connect() as conn:
            row = self._get(conn, cid)
            if not self._may(row, actor):
                raise Err(404, "not_found", "That collection was not found.")
            return self._collection_json(conn, row, actor)

    def apply(self, actor, manifest: dict, *, expected_revision) -> dict:
        """Create (``expected_revision`` "absent") or replace a collection's manifest."""
        try:
            m = Manifest.model_validate(manifest)
        except Exception as exc:  # noqa: BLE001 - the validator names the field
            errs = exc.errors() if hasattr(exc, "errors") else []
            first = errs[0] if errs else {}
            where = ".".join(str(x) for x in first.get("loc", ()))
            raise Err(422, "invalid_config", f"{where}: {first.get('msg', 'invalid')}" if where else "Invalid collection.",
                      fields={".".join(str(x) for x in e.get("loc", ())): e.get("msg") for e in errs[:20]})
        for s in m.sources:
            if s.kind == "upload" and not s.file_id:
                raise Err(422, "invalid_config", f"Source {s.id} needs file_id.")
            if s.kind in ("file", "directory") and not s.path:
                raise Err(422, "invalid_config", f"Source {s.id} needs path.")
            if s.kind == "text" and not (s.text or "").strip():
                raise Err(422, "invalid_config", f"Source {s.id} needs text.")
            if s.kind == "url" and not (s.url or "").startswith("https://"):
                raise Err(422, "invalid_config", f"Source {s.id} needs an https:// url.")
            if s.kind == "upload" and self.files is not None:
                self.files.get(actor, s.file_id)  # the person must be able to read the file (404 otherwise)
        body = m.model_dump()
        t = _now()
        with self.engine.begin() as conn:
            row = self._get(conn, m.id)
            current = None if row is None or row.deleted_at is not None else row.revision
            want = None if expected_revision in ("absent", None) else int(expected_revision)
            if want != current:
                raise Err(409, "revision_conflict", "The collection changed. Reload it and apply again.",
                          current_revision=current if current is not None else "absent")
            if row is not None and row.deleted_at is None and not self._may(row, actor, True):
                raise Err(403, "permission_denied", "You can read this collection but not change it.")
            if row is None:
                conn.execute(collections.insert().values(
                    hub_id=self.hub, id=m.id, owner_principal_id=actor.id, title=m.title, manifest_json=json.dumps(body),
                    revision=1, active_generation_id=None, created_at=t, updated_at=t, deleted_at=None))
            else:
                conn.execute(collections.update().where(collections.c.hub_id == self.hub, collections.c.id == m.id)
                             .values(title=m.title, manifest_json=json.dumps(body), revision=(current or 0) + 1,
                                     updated_at=t, deleted_at=None,
                                     owner_principal_id=row.owner_principal_id if row.deleted_at is None else actor.id))
            keep = {s.id for s in m.sources}
            # A source taken out of the manifest leaves results at once.
            conn.execute(sources.update().where(sources.c.hub_id == self.hub, sources.c.collection_id == m.id,
                                                sources.c.key.not_in(keep) if keep else sa.true(),
                                                sources.c.deleted_at.is_(None))
                         .values(deleted_at=t, state="deleted"))
            return self._collection_json(conn, self._get(conn, m.id), actor)

    def delete(self, actor, cid: str) -> None:
        with self.engine.begin() as conn:
            row = self._get(conn, cid)
            if row is None or row.deleted_at is not None or row.owner_principal_id != actor.id:
                raise Err(404, "not_found", "That collection was not found.")
            t = _now()
            conn.execute(collections.update().where(collections.c.hub_id == self.hub, collections.c.id == cid)
                         .values(deleted_at=t, active_generation_id=None, revision=row.revision + 1))
            conn.execute(sources.update().where(sources.c.hub_id == self.hub, sources.c.collection_id == cid)
                         .values(deleted_at=t, state="deleted"))

    # -- syncing ---------------------------------------------------------------------------------
    def start_sync(self, actor, cid: str, *, background: bool = True) -> dict:
        with self.engine.begin() as conn:
            row = self._get(conn, cid)
            if not self._may(row, actor, True):
                raise Err(404, "not_found", "That collection was not found.")
            running = conn.execute(sa.select(jobs).where(jobs.c.hub_id == self.hub, jobs.c.collection_id == cid,
                                                         jobs.c.state.in_(("queued", "extracting", "indexing")))).first()
            if running is not None:
                return self._job_json(running)
            jid = str(uuid.uuid4())
            t = _now()
            conn.execute(jobs.insert().values(id=jid, hub_id=self.hub, collection_id=cid, operation="sync",
                                              principal_id=actor.id, state="queued", counters_json="{}",
                                              error_code=None, error=None, created_at=t, updated_at=t))
        if background:
            threading.Thread(target=self._sync, args=(jid, actor, cid), daemon=True, name=f"hubzoid-sync-{cid}").start()
        else:
            self._sync(jid, actor, cid)
        return self.job(actor, jid)

    def _set_job(self, jid: str, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(jobs.update().where(jobs.c.id == jid).values(updated_at=_now(), **values))

    def _read_source(self, actor, s: SourceSpec) -> list[tuple[str, str, str, bytes]]:
        """[(key, title, locator, bytes...)] for one manifest source."""
        from .. import _fs
        from .. import uploads as uploads_lib

        cap = self.settings()["limits"]["max_source_bytes"]
        if s.kind == "text":
            return [(s.id, s.title or s.id, f"text:{s.id}", "text/markdown", s.text.encode())]
        if s.kind == "upload":
            desc, data = self.files.content(actor, s.file_id)
            return [(s.id, s.title or desc["name"], f"file:{s.file_id}", desc["mime_type"], data)]
        if s.kind == "url":
            lim = self.settings()["web"]
            final, mime, data = self.fetch(s.url, lim["limits"], allowed=lim["allowed_domains"],
                                           blocked=lim["blocked_domains"])
            return [(s.id, s.title or final, final, mime, data)]
        base = self.hub_dir.resolve()
        target = (self.hub_dir / s.path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise Err(422, "invalid_config", f"{s.path} is outside the hub.")
        paths = [target] if target.is_file() else sorted(
            p for p in (target.rglob("*") if s.recursive else target.glob("*")) if p.is_file())
        out = []
        for p in paths:
            if p.is_symlink() or any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            if _fs.agent_read_refusal(self.hub_dir, p) is not None:
                continue
            if p.stat().st_size > cap:
                continue
            rel = p.relative_to(base).as_posix()
            key = s.id if p == target else f"{s.id}/{p.relative_to(target).as_posix()}"
            out.append((key, rel, f"hub:{rel}", uploads_lib.guess_mime(p.name, fallback="text/plain"), p.read_bytes()))
        return out

    def _sync(self, jid: str, actor, cid: str) -> None:
        import hashlib

        cfg = self.settings()
        counters = {"sources": 0, "passages": 0, "failed": 0}
        try:
            self._set_job(jid, state="extracting")
            with self.engine.connect() as conn:
                row = self._get(conn, cid)
            manifest = Manifest.model_validate(json.loads(row.manifest_json))
            revision = row.revision
            built: list[tuple[str, dict, list[tuple[str, dict]], str]] = []  # (key, meta, passages, sha)
            failures: dict[str, str] = {}
            for spec in manifest.sources:
                try:
                    for key, title, locator, mime, data in self._read_source(actor, spec):
                        _t, pages = extract(title, mime, data)
                        passages = chunk(pages, cfg["chunking"]["tokens"], cfg["chunking"]["overlap"])
                        built.append((key, {"kind": spec.kind, "title": (_t if spec.kind == "url" else title)[:255],
                                            "locator": locator, "file_id": spec.file_id},
                                      passages, hashlib.sha256(data).hexdigest()))
                except Err as exc:
                    failures[spec.id] = exc.message
                except Exception as exc:  # noqa: BLE001 - one bad source never stops the rest
                    failures[spec.id] = type(exc).__name__
            self._set_job(jid, state="indexing")
            gid = str(uuid.uuid4())
            fp = canonical.digest({"rev": revision, "chunking": cfg["chunking"],
                                   "sources": sorted((k, sha) for k, _m, _p, sha in built)})
            t = _now()
            with self.engine.begin() as conn:
                current = self._get(conn, cid)
                if current is None or current.revision != revision or current.deleted_at is not None:
                    raise Err(409, "revision_conflict", "The collection changed during the sync. Sync again.")
                conn.execute(generations.insert().values(id=gid, hub_id=self.hub, collection_id=cid,
                                                         manifest_revision=revision, fingerprint=fp, state="building",
                                                         created_at=t, published_at=None))
                fts = conn.dialect.name == "sqlite"
                for key, meta, passages, sha in built:
                    existing = conn.execute(sa.select(sources).where(sources.c.hub_id == self.hub,
                                                                     sources.c.collection_id == cid,
                                                                     sources.c.key == key)).first()
                    if existing is None:
                        sid, ver = str(uuid.uuid4()), 1
                        conn.execute(sources.insert().values(
                            source_id=sid, hub_id=self.hub, collection_id=cid, key=key, kind=meta["kind"],
                            title=meta["title"], locator=meta["locator"], source_version=1, content_sha256=sha,
                            file_id=meta["file_id"], state="ready", error=None, observed_at=t, deleted_at=None))
                    else:
                        sid = existing.source_id
                        ver = existing.source_version + (1 if existing.content_sha256 != sha else 0)
                        conn.execute(sources.update().where(sources.c.source_id == sid).values(
                            title=meta["title"], locator=meta["locator"], source_version=ver, content_sha256=sha,
                            state="ready", error=None, observed_at=t, deleted_at=None, kind=meta["kind"]))
                    for n, (text, loc) in enumerate(passages):
                        ch = canonical.digest([gid, sid, ver, n])[7:39]
                        conn.execute(chunks.insert().values(chunk_id=ch, generation_id=gid, source_id=sid,
                                                            source_version=ver, ordinal=n, locator_json=json.dumps(loc),
                                                            text=text))
                        if fts:
                            conn.exec_driver_sql(f"INSERT INTO {FTS}(chunk_id, text) VALUES (?, ?)", (ch, text))
                    counters["sources"] += 1
                    counters["passages"] += len(passages)
                for key, why in failures.items():
                    conn.execute(sources.update().where(sources.c.hub_id == self.hub, sources.c.collection_id == cid,
                                                        sources.c.key == key).values(state="failed", error=why))
                counters["failed"] = len(failures)
                # Sources whose read failed this time keep their previous passages:
                # a partial sync never removes what was there.
                old = current.active_generation_id
                if old and failures:
                    keep = [r.source_id for r in conn.execute(sa.select(sources.c.source_id).where(
                        sources.c.hub_id == self.hub, sources.c.collection_id == cid,
                        sources.c.key.in_(list(failures)), sources.c.deleted_at.is_(None)))]
                    for c in conn.execute(sa.select(chunks).where(chunks.c.generation_id == old,
                                                                  chunks.c.source_id.in_(keep))):
                        ch = canonical.digest([gid, c.source_id, c.source_version, c.ordinal])[7:39]
                        conn.execute(chunks.insert().values(chunk_id=ch, generation_id=gid, source_id=c.source_id,
                                                            source_version=c.source_version, ordinal=c.ordinal,
                                                            locator_json=c.locator_json, text=c.text))
                        if fts:
                            conn.exec_driver_sql(f"INSERT INTO {FTS}(chunk_id, text) VALUES (?, ?)", (ch, c.text))
                conn.execute(generations.update().where(generations.c.id == gid).values(state="active", published_at=t))
                conn.execute(collections.update().where(collections.c.hub_id == self.hub, collections.c.id == cid)
                             .values(active_generation_id=gid, updated_at=t))
                if old:
                    conn.execute(generations.update().where(generations.c.id == old).values(state="retired"))
            self._set_job(jid, state="ready", counters_json=json.dumps(counters))
        except Err as exc:
            self._set_job(jid, state="failed", error_code=exc.code, error=exc.message, counters_json=json.dumps(counters))
        except Exception as exc:  # noqa: BLE001
            log.exception("retrieval: sync of %s failed", cid)
            self._set_job(jid, state="failed", error_code="processor_unavailable", error=type(exc).__name__,
                          counters_json=json.dumps(counters))

    @staticmethod
    def _job_json(r) -> dict:
        return {"job_id": r.id, "collection_id": r.collection_id, "operation": r.operation, "state": r.state,
                "counters": json.loads(r.counters_json or "{}"), "error_code": r.error_code, "error": r.error,
                "updated_at": r.updated_at}

    def job(self, actor, jid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(jobs).where(jobs.c.id == jid, jobs.c.hub_id == self.hub)).first()
            row = self._get(conn, r.collection_id) if r else None
            if r is None or not (r.principal_id == actor.id or self._may(row, actor)):
                raise Err(404, "not_found", "That job was not found.")
            return self._job_json(r)

    # -- querying ----------------------------------------------------------------------------------
    def readable_generations(self, conn, actor, collection_ids: list[str] | None, agent: str | None) -> dict[str, str]:
        rows = conn.execute(sa.select(collections).where(collections.c.hub_id == self.hub,
                                                         collections.c.deleted_at.is_(None),
                                                         collections.c.active_generation_id.is_not(None))).all()
        out = {}
        for r in rows:
            if collection_ids and r.id not in collection_ids:
                continue
            if not self._may(r, actor):
                continue
            agents = json.loads(r.manifest_json).get("agents") or []
            if agent and agents and agent not in agents:
                continue
            out[r.id] = r.active_generation_id
        return out

    def query(self, actor, text: str, *, collection_ids: list[str] | None = None, top_k: int | None = None,
              agent: str | None = None, conversation_id: str | None = None, run_id: str | None = None) -> dict:
        cfg = self.settings()
        if not cfg["enabled"]:
            raise Err(403, "feature_disabled", "Sources are turned off for this deployment.")
        text = (text or "").strip()
        if not text or len(text) > cfg["limits"]["query_chars"]:
            raise Err(422, "validation_failed", f"Ask in 1 to {cfg['limits']['query_chars']} characters.")
        if collection_ids and len(collection_ids) > cfg["limits"]["collections_per_query"]:
            raise Err(422, "validation_failed", "Too many collections in one query.")
        k = max(1, min(int(top_k or cfg["search"]["top_k"]), 50))
        from .search import _fts_expression, parse_query

        words, phrases, _prefix = parse_query(text, cfg["limits"]["query_chars"])
        words = [w for w in dict.fromkeys(w.lower() for w in words) if w not in STOP and len(w) > 1]
        with self.engine.begin() as conn:
            gens = self.readable_generations(conn, actor, collection_ids, agent)
            if not gens or not (words or phrases):
                return {"query": text, "results": [], "no_matches": True, "collections": sorted(gens)}
            live = sa.select(sources.c.source_id).where(sources.c.hub_id == self.hub, sources.c.deleted_at.is_(None))
            base = sa.select(chunks, sources.c.title, sources.c.locator, sources.c.collection_id, sources.c.kind).join(
                sources, sources.c.source_id == chunks.c.source_id).where(
                chunks.c.generation_id.in_(list(gens.values())), chunks.c.source_id.in_(live))
            if conn.dialect.name == "sqlite":
                # Any of the words (BM25 ranks passages with more of them first).
                expr = " OR ".join(_fts_expression([w], [], None) for w in words) or _fts_expression([], phrases, None)
                hits = sa.text(f"SELECT chunk_id AS cid, bm25({FTS}) AS score FROM {FTS} WHERE {FTS} MATCH :q").bindparams(
                    q=expr).columns(sa.column("cid"), sa.column("score")).subquery()
                rows = conn.execute(base.add_columns(hits.c.score).join(hits, hits.c.cid == chunks.c.chunk_id)
                                    .order_by(hits.c.score, chunks.c.source_id, chunks.c.ordinal).limit(k)).all()
            else:
                tsq = sa.func.to_tsquery("simple", " | ".join(words) or " & ".join(phrases))
                vec = sa.func.to_tsvector("simple", chunks.c.text)
                rank = sa.func.ts_rank(vec, tsq)
                rows = conn.execute(base.add_columns(rank.label("score")).where(vec.op("@@")(tsq))
                                    .order_by(rank.desc(), chunks.c.source_id, chunks.c.ordinal).limit(k)).all()
            results = []
            t = _now()
            for r in rows:
                cit = str(uuid.uuid4())
                conn.execute(citations.insert().values(
                    citation_id=cit, hub_id=self.hub, collection_id=r.collection_id, source_id=r.source_id,
                    source_version=r.source_version, chunk_id=r.chunk_id, generation_id=r.generation_id,
                    principal_id=actor.id, conversation_id=conversation_id, run_id=run_id, created_at=t))
                results.append({"citation_id": cit, "collection_id": r.collection_id, "source_id": r.source_id,
                                "source_version": r.source_version, "title": r.title, "kind": r.kind,
                                "url": r.locator if r.kind == "url" else None,
                                "locator": json.loads(r.locator_json), "excerpt": r.text[:EXCERPT],
                                "score": float(r.score) if r.score is not None else None})
        return {"query": text, "results": results, "no_matches": not results, "collections": sorted(gens)}

    def citation(self, actor, citation_id: str) -> dict:
        """A stored citation, re-authorised now: the source must still exist and
        be readable by this person, at the version that was cited."""
        with self.engine.connect() as conn:
            c = conn.execute(sa.select(citations).where(citations.c.citation_id == citation_id,
                                                        citations.c.hub_id == self.hub)).first()
            row = self._get(conn, c.collection_id) if c else None
            src = conn.execute(sa.select(sources).where(sources.c.source_id == c.source_id)).first() if c else None
            ch = conn.execute(sa.select(chunks).where(chunks.c.chunk_id == c.chunk_id)).first() if c else None
        if c is None or not self._may(row, actor) or src is None or ch is None:
            raise Err(404, "citation_unavailable", "This source is no longer available.")
        if src.deleted_at is not None:
            raise Err(404, "citation_unavailable", "This source is no longer available.")
        return {"citation_id": c.citation_id, "title": src.title, "kind": src.kind,
                "url": src.locator if src.kind == "url" else None, "collection_id": c.collection_id,
                "source_version": c.source_version, "current_version": src.source_version,
                "changed_since": src.source_version != c.source_version, "locator": json.loads(ch.locator_json),
                "passage": ch.text[:4000]}


_services: dict[str, RetrievalService] = {}


def service_for(hub_dir: Path, engine, hub: str, files=None) -> RetrievalService:
    key = str(Path(hub_dir).resolve())
    svc = _services.get(key)
    if svc is None or svc.engine is not engine:
        svc = RetrievalService(hub_dir, engine, hub, files)
        _services[key] = svc
    elif files is not None:
        svc.files = files
    return svc
