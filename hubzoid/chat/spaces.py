"""Team spaces: channels and direct messages, notes, and a calendar (HUB-80, first part).

**Channels** have members (or are open to everyone using the agent) and posts
with threads. Every post carries its verified author (a person, or the agent
when someone mentions it); edits record the editor and keep a revision, and a
deleted post leaves a tombstone. People react, pin, mention each other (an
inbox notice) and mute a channel. Mentioning the agent (``@agent``) makes it
answer in the thread, as the person who asked: their identity and grants, never
the channel's.

**Notes** are personal or belong to a channel. Saving needs the revision you
edited; a stale save is refused with the newer text, so you can keep both (save
yours as a copy) or reload. Every revision is kept and can be restored.

**Calendar** events (personal or a channel's) have a time zone, attendees and a
simple repeat (none, daily, weekly). Reminders are delivered through the
notification inbox by the bridge's own loop: due reminders fire once, survive
restarts (state is in the database), are cancelled when the event is deleted,
and are dropped for someone who can no longer see the event.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from . import graph as g

log = logging.getLogger("hubzoid.chat.spaces")
metadata = sa.MetaData()
Err = g.GraphError
REACTIONS = ("👍", "✅", "👀", "❤️", "🎉", "🙏")
MENTION = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|agent)\b")
MAX_POST = 20_000

spaces = sa.Table(
    "hz_spaces", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("kind", sa.String(16), nullable=False),          # channel | dm
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("open", sa.Integer, nullable=False),             # everyone using the agent may read and post
    sa.Column("created_by", sa.String(64), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("archived_at", sa.Float),
)

members = sa.Table(
    "hz_space_members", metadata,
    sa.Column("space_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),          # owner | member
    sa.Column("muted", sa.Integer, nullable=False),
    sa.Column("last_read_at", sa.Float),
    sa.Column("added_at", sa.Float, nullable=False),
)

posts = sa.Table(
    "hz_space_posts", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("space_id", sa.String(64), nullable=False, index=True),
    sa.Column("parent_id", sa.String(64)),
    sa.Column("author_id", sa.String(64), nullable=False),
    sa.Column("author_kind", sa.String(16), nullable=False),   # person | agent
    sa.Column("on_behalf_of", sa.String(64)),                  # an agent post: who asked
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("editor_id", sa.String(64)),
    sa.Column("state", sa.String(16), nullable=False),         # posted | pending | failed | deleted
    sa.Column("pinned_by", sa.String(64)),
    sa.Column("pinned_at", sa.Float),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

reactions = sa.Table(
    "hz_space_reactions", metadata,
    sa.Column("post_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("emoji", sa.String(16), primary_key=True),
    sa.Column("created_at", sa.Float, nullable=False),
)

notes = sa.Table(
    "hz_notes", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("space_id", sa.String(64)),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_by", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)

note_revisions = sa.Table(
    "hz_note_revisions", metadata,
    sa.Column("note_id", sa.String(64), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("author_id", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

events = sa.Table(
    "hz_calendar_events", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("space_id", sa.String(64)),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("starts_at", sa.Float, nullable=False),
    sa.Column("ends_at", sa.Float, nullable=False),
    sa.Column("timezone", sa.String(64), nullable=False),
    sa.Column("repeat", sa.String(16), nullable=False),        # none | daily | weekly
    sa.Column("repeat_until", sa.Float),
    sa.Column("attendees_json", sa.Text, nullable=False),
    sa.Column("details", sa.Text),
    sa.Column("reminder_minutes", sa.Integer),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)

reminders = sa.Table(
    "hz_calendar_reminders", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("event_id", sa.String(64), nullable=False, index=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("occurrence_at", sa.Float, nullable=False),
    sa.Column("due_at", sa.Float, nullable=False, index=True),
    sa.Column("state", sa.String(16), nullable=False),         # pending | sent | cancelled
    sa.Column("sent_at", sa.Float),
    sa.UniqueConstraint("event_id", "user_id", "occurrence_at", name="uq_hz_reminder_occurrence"),
)

TABLES = (spaces, members, posts, reactions, notes, note_revisions, events, reminders)


def _now() -> float:
    return time.time()


class SpacesService:
    def __init__(self, hub_dir: Path, engine, hub: str):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub
        #: (recipient_id, kind, source_event_id, ref) -> None: an inbox notice (HUB-76 inbox)
        self.notify = lambda *a, **k: None
        #: called after a change commits: changed(space_id)
        self.changed = lambda space_id: None

    # -- people --------------------------------------------------------------------------------
    def _user(self, ref: str) -> dict | None:
        from ..auth import users

        return users.get(self.hub_dir, ref) or users.find_by_email(self.hub_dir, ref)

    # -- spaces --------------------------------------------------------------------------------
    def _space(self, conn, sid: str):
        s = conn.execute(sa.select(spaces).where(spaces.c.id == sid, spaces.c.hub_id == self.hub)).first()
        if s is None or s.archived_at is not None:
            raise Err(404, "not_found", "That space was not found.")
        return s

    def _role(self, conn, s, actor) -> str | None:
        m = conn.execute(sa.select(members.c.role).where(members.c.space_id == s.id,
                                                         members.c.user_id == actor.id)).scalar()
        if m:
            return m
        return "member" if s.open else None

    def _readable(self, conn, sid: str, actor):
        s = self._space(conn, sid)
        role = self._role(conn, s, actor)
        if role is None:
            raise Err(404, "not_found", "That space was not found.")
        return s, role

    def create_space(self, actor, *, name: str, kind: str = "channel", people: list[str] | None = None,
                     open_: bool = False) -> dict:
        if kind not in ("channel", "dm"):
            raise Err(422, "validation_failed", "kind is channel or dm.")
        name = (name or "").strip()
        ids = []
        for ref in people or []:
            u = self._user(str(ref))
            if u is None:
                raise Err(422, "principal_not_found", f"No account {ref!r} in this deployment.")
            if u["id"] != actor.id:
                ids.append(u["id"])
        if kind == "dm":
            if len(ids) != 1:
                raise Err(422, "validation_failed", "A direct message is with one other person.")
            open_ = False
            other = self._user(ids[0])
            name = name or (other.get("name") or other.get("email"))
        elif not name:
            raise Err(422, "validation_failed", "A channel needs a name.")
        sid = str(uuid.uuid4())
        t = _now()
        with self.engine.begin() as conn:
            if kind == "dm":  # one direct message per pair
                mine = sa.select(members.c.space_id).where(members.c.user_id == actor.id)
                theirs = sa.select(members.c.space_id).where(members.c.user_id == ids[0])
                existing = conn.execute(sa.select(spaces).where(spaces.c.hub_id == self.hub, spaces.c.kind == "dm",
                                                                spaces.c.id.in_(mine), spaces.c.id.in_(theirs),
                                                                spaces.c.archived_at.is_(None))).first()
                if existing is not None:
                    return self._space_json(conn, existing, actor)
            conn.execute(spaces.insert().values(id=sid, hub_id=self.hub, kind=kind, name=name[:120],
                                                open=1 if open_ else 0, created_by=actor.id, revision=1,
                                                created_at=t, archived_at=None))
            conn.execute(members.insert().values(space_id=sid, user_id=actor.id, role="owner", muted=0,
                                                 last_read_at=t, added_at=t))
            for uid in ids:
                conn.execute(members.insert().values(space_id=sid, user_id=uid, role="member", muted=0,
                                                     last_read_at=None, added_at=t))
            return self._space_json(conn, self._space(conn, sid), actor)

    def _space_json(self, conn, s, actor) -> dict:
        ms = conn.execute(sa.select(members).where(members.c.space_id == s.id)).all()
        me = next((m for m in ms if m.user_id == actor.id), None)
        last = conn.execute(sa.select(sa.func.max(posts.c.created_at)).where(posts.c.space_id == s.id)).scalar()
        unread = conn.execute(sa.select(sa.func.count()).select_from(posts).where(
            posts.c.space_id == s.id, posts.c.state == "posted", posts.c.author_id != actor.id,
            posts.c.created_at > ((me.last_read_at or 0) if me else _now()))).scalar() or 0
        people = []
        for m in ms:
            u = self._user(m.user_id) or {}
            people.append({"user_id": m.user_id, "name": u.get("name") or u.get("email") or "Someone",
                           "email": u.get("email"), "role": m.role})
        return {"id": s.id, "kind": s.kind, "name": s.name, "open": bool(s.open), "revision": s.revision,
                "members": people, "muted": bool(me.muted) if me else False, "role": self._role(conn, s, actor),
                "last_post_at": last, "unread": unread if me else 0}

    def list_spaces(self, actor) -> list[dict]:
        with self.engine.connect() as conn:
            mine = sa.select(members.c.space_id).where(members.c.user_id == actor.id)
            rows = conn.execute(sa.select(spaces).where(spaces.c.hub_id == self.hub, spaces.c.archived_at.is_(None),
                                                        sa.or_(spaces.c.id.in_(mine), spaces.c.open == 1))
                                .order_by(spaces.c.created_at)).all()
            return [self._space_json(conn, r, actor) for r in rows]

    def set_members(self, actor, sid: str, people: list[str], expected_revision: int) -> dict:
        with self.engine.begin() as conn:
            s, role = self._readable(conn, sid, actor)
            if role != "owner" or s.kind == "dm":
                raise Err(403, "permission_denied", "Only the channel's owner changes its members.")
            if s.revision != expected_revision:
                raise Err(409, "revision_conflict", "Members changed. Reload.", current_revision=s.revision)
            want = {actor.id}
            for ref in people:
                u = self._user(str(ref))
                if u is None:
                    raise Err(422, "principal_not_found", f"No account {ref!r} in this deployment.")
                want.add(u["id"])
            have = {r.user_id for r in conn.execute(sa.select(members.c.user_id).where(members.c.space_id == sid))}
            t = _now()
            for uid in want - have:
                conn.execute(members.insert().values(space_id=sid, user_id=uid, role="member", muted=0,
                                                     last_read_at=None, added_at=t))
            gone = have - want
            if gone:
                conn.execute(members.delete().where(members.c.space_id == sid, members.c.user_id.in_(gone)))
                # Someone removed stops getting this channel's reminders.
                ev = sa.select(events.c.id).where(events.c.space_id == sid)
                conn.execute(reminders.update().where(reminders.c.event_id.in_(ev), reminders.c.user_id.in_(gone),
                                                      reminders.c.state == "pending").values(state="cancelled"))
            conn.execute(spaces.update().where(spaces.c.id == sid).values(revision=s.revision + 1))
            out = self._space_json(conn, self._space(conn, sid), actor)
        self.changed(sid)
        return out

    def mute(self, actor, sid: str, muted: bool) -> dict:
        with self.engine.begin() as conn:
            s, _ = self._readable(conn, sid, actor)
            n = conn.execute(members.update().where(members.c.space_id == sid, members.c.user_id == actor.id)
                             .values(muted=1 if muted else 0)).rowcount
            if not n:
                conn.execute(members.insert().values(space_id=sid, user_id=actor.id, role="member",
                                                     muted=1 if muted else 0, last_read_at=_now(), added_at=_now()))
            return self._space_json(conn, s, actor)

    # -- posts ---------------------------------------------------------------------------------
    def _post_json(self, conn, p, actor) -> dict:
        rs: dict[str, list[str]] = {}
        for r in conn.execute(sa.select(reactions).where(reactions.c.post_id == p.id)):
            rs.setdefault(r.emoji, []).append(r.user_id)
        replies = conn.execute(sa.select(sa.func.count()).select_from(posts).where(
            posts.c.parent_id == p.id, posts.c.state != "deleted")).scalar() or 0
        if p.author_kind == "agent":
            author = {"kind": "agent", "name": "Agent", "on_behalf_of": p.on_behalf_of}
        else:
            u = self._user(p.author_id) or {}
            author = {"kind": "person", "id": p.author_id, "name": u.get("name") or u.get("email") or "Someone"}
        deleted = p.state == "deleted"
        return {"id": p.id, "space_id": p.space_id, "parent_id": p.parent_id, "author": author,
                "body": "" if deleted else p.body, "state": p.state, "revision": p.revision,
                "edited": p.revision > 1 and not deleted, "pinned": p.pinned_at is not None,
                "reactions": [{"emoji": e, "count": len(u), "mine": actor.id in u} for e, u in rs.items()],
                "replies": replies, "created_at": p.created_at, "mine": p.author_id == actor.id}

    def posts(self, actor, sid: str, *, parent_id: str | None = None, limit: int = 100) -> dict:
        with self.engine.begin() as conn:
            s, _ = self._readable(conn, sid, actor)
            q = sa.select(posts).where(posts.c.space_id == sid)
            q = q.where(posts.c.parent_id == parent_id) if parent_id else q.where(posts.c.parent_id.is_(None))
            rows = conn.execute(q.order_by(posts.c.created_at.desc()).limit(max(1, min(limit, 200)))).all()
            conn.execute(members.update().where(members.c.space_id == sid, members.c.user_id == actor.id)
                         .values(last_read_at=_now()))
            pinned = conn.execute(sa.select(posts).where(posts.c.space_id == sid, posts.c.pinned_at.is_not(None),
                                                         posts.c.state != "deleted")).all()
            return {"items": [self._post_json(conn, p, actor) for p in reversed(rows)],
                    "pinned": [self._post_json(conn, p, actor) for p in pinned]}

    def post(self, actor, sid: str, body: str, *, parent_id: str | None = None) -> tuple[dict, bool]:
        """Post (or reply). Returns the post and whether the agent was mentioned."""
        body = (body or "").strip()
        if not body or len(body) > MAX_POST:
            raise Err(422, "validation_failed", f"A post is 1 to {MAX_POST:,} characters.")
        t = _now()
        with self.engine.begin() as conn:
            s, _ = self._readable(conn, sid, actor)
            if parent_id:
                parent = conn.execute(sa.select(posts).where(posts.c.id == parent_id, posts.c.space_id == sid)).first()
                if parent is None or parent.parent_id is not None:
                    raise Err(422, "validation_failed", "Reply to a post of this space (threads are one level).")
            pid = str(uuid.uuid4())
            conn.execute(posts.insert().values(id=pid, space_id=sid, parent_id=parent_id, author_id=actor.id,
                                               author_kind="person", on_behalf_of=None, body=body, revision=1,
                                               editor_id=None, state="posted", pinned_by=None, pinned_at=None,
                                               created_at=t, updated_at=t))
            mentioned = {m.group(1).lower() for m in MENTION.finditer(body)}
            notify = []
            for ref in mentioned - {"agent"}:
                u = self._user(ref)
                if u is None or u["id"] == actor.id:
                    continue
                if self._role(conn, s, type("A", (), {"id": u["id"]})()) is None:
                    continue  # never notify someone who can't open the space
                muted = conn.execute(sa.select(members.c.muted).where(members.c.space_id == sid,
                                                                      members.c.user_id == u["id"])).scalar()
                if not muted:
                    notify.append(u["id"])
            out = self._post_json(conn, conn.execute(sa.select(posts).where(posts.c.id == pid)).first(), actor)
        for uid in notify:
            self.notify(uid, "space.mention", f"space.mention:{pid}:{uid}", f"space:{sid}:{pid}")
        self.changed(sid)
        return out, "agent" in mentioned

    def agent_reply_start(self, sid: str, parent_id: str, asker_id: str) -> str:
        t = _now()
        pid = str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(posts.insert().values(id=pid, space_id=sid, parent_id=parent_id, author_id="agent",
                                               author_kind="agent", on_behalf_of=asker_id, body="…", revision=1,
                                               editor_id=None, state="pending", pinned_by=None, pinned_at=None,
                                               created_at=t, updated_at=t))
        self.changed(sid)
        return pid

    def agent_reply_finish(self, pid: str, body: str, ok: bool) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(posts.c.space_id).where(posts.c.id == pid)).first()
            conn.execute(posts.update().where(posts.c.id == pid).values(
                body=body[:MAX_POST] or "(no answer)", state="posted" if ok else "failed", updated_at=_now()))
        if row:
            self.changed(row.space_id)

    def thread_text(self, sid: str, parent_id: str) -> str:
        with self.engine.connect() as conn:
            root = conn.execute(sa.select(posts).where(posts.c.id == parent_id)).first()
            rows = [root] + conn.execute(sa.select(posts).where(posts.c.parent_id == parent_id,
                                                                posts.c.state == "posted")
                                         .order_by(posts.c.created_at)).all()
        out = []
        for p in rows:
            if p is None or p.state == "deleted":
                continue
            who = "agent" if p.author_kind == "agent" else ((self._user(p.author_id) or {}).get("name") or "someone")
            out.append(f"[{who}]\n{p.body}")
        return "\n\n".join(out)

    def edit(self, actor, pid: str, body: str, expected_revision: int) -> dict:
        body = (body or "").strip()
        if not body or len(body) > MAX_POST:
            raise Err(422, "validation_failed", "A post is 1 to 20,000 characters.")
        with self.engine.begin() as conn:
            p = conn.execute(sa.select(posts).where(posts.c.id == pid)).first()
            if p is None:
                raise Err(404, "not_found", "That post was not found.")
            self._readable(conn, p.space_id, actor)
            if p.author_id != actor.id or p.state != "posted":
                raise Err(403, "permission_denied", "You can only edit your own posts.")
            if p.revision != expected_revision:
                raise Err(409, "revision_conflict", "The post changed. Reload it.", current_revision=p.revision)
            conn.execute(posts.update().where(posts.c.id == pid).values(body=body, revision=p.revision + 1,
                                                                        editor_id=actor.id, updated_at=_now()))
            out = self._post_json(conn, conn.execute(sa.select(posts).where(posts.c.id == pid)).first(), actor)
        self.changed(p.space_id)
        return out

    def delete_post(self, actor, pid: str) -> None:
        with self.engine.begin() as conn:
            p = conn.execute(sa.select(posts).where(posts.c.id == pid)).first()
            if p is None:
                return
            _s, role = self._readable(conn, p.space_id, actor)
            if p.author_id != actor.id and role != "owner":
                raise Err(403, "permission_denied", "Only the author or the channel owner deletes a post.")
            conn.execute(posts.update().where(posts.c.id == pid).values(state="deleted", body="", pinned_at=None,
                                                                        updated_at=_now()))
            conn.execute(reactions.delete().where(reactions.c.post_id == pid))
        self.changed(p.space_id)

    def react(self, actor, pid: str, emoji: str, on: bool) -> dict:
        if emoji not in REACTIONS:
            raise Err(422, "validation_failed", f"Reactions are {' '.join(REACTIONS)}.")
        with self.engine.begin() as conn:
            p = conn.execute(sa.select(posts).where(posts.c.id == pid)).first()
            if p is None or p.state == "deleted":
                raise Err(404, "not_found", "That post was not found.")
            self._readable(conn, p.space_id, actor)
            conn.execute(reactions.delete().where(reactions.c.post_id == pid, reactions.c.user_id == actor.id,
                                                  reactions.c.emoji == emoji))
            if on:
                conn.execute(reactions.insert().values(post_id=pid, user_id=actor.id, emoji=emoji, created_at=_now()))
            out = self._post_json(conn, p, actor)
        self.changed(p.space_id)
        return out

    def pin(self, actor, pid: str, on: bool) -> dict:
        with self.engine.begin() as conn:
            p = conn.execute(sa.select(posts).where(posts.c.id == pid)).first()
            if p is None or p.state == "deleted":
                raise Err(404, "not_found", "That post was not found.")
            self._readable(conn, p.space_id, actor)
            conn.execute(posts.update().where(posts.c.id == pid).values(
                pinned_by=actor.id if on else None, pinned_at=_now() if on else None))
            out = self._post_json(conn, conn.execute(sa.select(posts).where(posts.c.id == pid)).first(), actor)
        self.changed(p.space_id)
        return out

    # -- notes -------------------------------------------------------------------------------------
    def _note(self, conn, nid: str, actor):
        n = conn.execute(sa.select(notes).where(notes.c.id == nid, notes.c.hub_id == self.hub)).first()
        if n is None or n.deleted_at is not None:
            raise Err(404, "not_found", "That note was not found.")
        if n.space_id:
            self._readable(conn, n.space_id, actor)
        elif n.owner_id != actor.id:
            raise Err(404, "not_found", "That note was not found.")
        return n

    @staticmethod
    def _note_json(n) -> dict:
        return {"id": n.id, "space_id": n.space_id, "title": n.title, "body": n.body, "revision": n.revision,
                "owner_id": n.owner_id, "updated_by": n.updated_by, "updated_at": n.updated_at}

    def list_notes(self, actor, space_id: str | None = None) -> list[dict]:
        with self.engine.connect() as conn:
            q = sa.select(notes).where(notes.c.hub_id == self.hub, notes.c.deleted_at.is_(None))
            if space_id:
                self._readable(conn, space_id, actor)
                q = q.where(notes.c.space_id == space_id)
            else:
                q = q.where(notes.c.space_id.is_(None), notes.c.owner_id == actor.id)
            return [self._note_json(n) for n in conn.execute(q.order_by(notes.c.updated_at.desc()))]

    def create_note(self, actor, title: str, body: str, space_id: str | None = None) -> dict:
        nid = str(uuid.uuid4())
        t = _now()
        title = (title or "").strip()[:200] or "Untitled note"
        with self.engine.begin() as conn:
            if space_id:
                self._readable(conn, space_id, actor)
            conn.execute(notes.insert().values(id=nid, hub_id=self.hub, owner_id=actor.id, space_id=space_id,
                                               title=title, body=body or "", revision=1, updated_by=actor.id,
                                               updated_at=t, deleted_at=None))
            conn.execute(note_revisions.insert().values(note_id=nid, revision=1, title=title, body=body or "",
                                                        author_id=actor.id, created_at=t))
            return self._note_json(self._note(conn, nid, actor))

    def save_note(self, actor, nid: str, *, title: str, body: str, expected_revision: int) -> dict:
        """Save the revision you edited. A stale save is refused with the newer text."""
        t = _now()
        with self.engine.begin() as conn:
            n = self._note(conn, nid, actor)
            if n.revision != expected_revision:
                raise Err(409, "revision_conflict", "Someone saved this note while you were editing. Keep both, or "
                                                    "reload theirs.", current=self._note_json(n))
            title = (title or "").strip()[:200] or n.title
            conn.execute(notes.update().where(notes.c.id == nid).values(title=title, body=body or "",
                                                                        revision=n.revision + 1, updated_by=actor.id,
                                                                        updated_at=t))
            conn.execute(note_revisions.insert().values(note_id=nid, revision=n.revision + 1, title=title,
                                                        body=body or "", author_id=actor.id, created_at=t))
            return self._note_json(self._note(conn, nid, actor))

    def note_revisions(self, actor, nid: str) -> list[dict]:
        with self.engine.connect() as conn:
            self._note(conn, nid, actor)
            return [{"revision": r.revision, "title": r.title, "body": r.body, "author_id": r.author_id,
                     "created_at": r.created_at} for r in conn.execute(
                sa.select(note_revisions).where(note_revisions.c.note_id == nid)
                .order_by(note_revisions.c.revision.desc()))]

    def restore_note(self, actor, nid: str, revision: int, expected_revision: int) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(note_revisions).where(note_revisions.c.note_id == nid,
                                                             note_revisions.c.revision == revision)).first()
        if r is None:
            raise Err(404, "not_found", "That revision was not found.")
        return self.save_note(actor, nid, title=r.title, body=r.body, expected_revision=expected_revision)

    def delete_note(self, actor, nid: str) -> None:
        with self.engine.begin() as conn:
            n = self._note(conn, nid, actor)
            if n.owner_id != actor.id:
                raise Err(403, "permission_denied", "Only the note's owner deletes it.")
            conn.execute(notes.update().where(notes.c.id == nid).values(deleted_at=_now()))

    # -- calendar ------------------------------------------------------------------------------------
    def _event(self, conn, eid: str, actor):
        e = conn.execute(sa.select(events).where(events.c.id == eid, events.c.hub_id == self.hub)).first()
        if e is None or e.deleted_at is not None or not self._may_see(conn, e, actor.id):
            raise Err(404, "not_found", "That event was not found.")
        return e

    def _may_see(self, conn, e, user_id: str) -> bool:
        if e.owner_id == user_id or user_id in json.loads(e.attendees_json):
            return True
        if e.space_id:
            try:
                s = self._space(conn, e.space_id)
            except Err:
                return False
            return self._role(conn, s, type("A", (), {"id": user_id})()) is not None
        return False

    @staticmethod
    def occurrences(e, start: float, end: float) -> list[float]:
        """Start times of an event between ``start`` and ``end``, repeating in its own time zone."""
        if e.repeat == "none":
            return [e.starts_at] if start <= e.starts_at < end else []
        tz = ZoneInfo(e.timezone)
        first = datetime.fromtimestamp(e.starts_at, tz)
        step = timedelta(days=1 if e.repeat == "daily" else 7)
        out, n = [], 0
        until = e.repeat_until or float("inf")
        while n < 1000:
            at = (first + step * n)  # wall-clock repeat: the same local time across DST changes
            ts = datetime(at.year, at.month, at.day, first.hour, first.minute, tzinfo=tz).timestamp()
            if ts >= end or ts > until:
                break
            if ts >= start:
                out.append(ts)
            n += 1
        return out

    def _event_json(self, e, actor) -> dict:
        return {"id": e.id, "title": e.title, "space_id": e.space_id, "starts_at": e.starts_at, "ends_at": e.ends_at,
                "timezone": e.timezone, "repeat": e.repeat, "repeat_until": e.repeat_until,
                "attendees": json.loads(e.attendees_json), "details": e.details,
                "reminder_minutes": e.reminder_minutes, "revision": e.revision, "mine": e.owner_id == actor.id}

    def save_event(self, actor, body: dict, *, event_id: str | None = None) -> dict:
        title = str(body.get("title") or "").strip()[:200]
        if not title:
            raise Err(422, "validation_failed", "An event needs a title.")
        try:
            starts, ends = float(body["starts_at"]), float(body["ends_at"])
        except (KeyError, TypeError, ValueError):
            raise Err(422, "validation_failed", "starts_at and ends_at are times (seconds since 1970, UTC).")
        if ends <= starts:
            raise Err(422, "validation_failed", "An event ends after it starts.")
        tz = str(body.get("timezone") or "UTC")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise Err(422, "validation_failed", f"{tz} is not a time zone.")
        repeat = body.get("repeat") or "none"
        if repeat not in ("none", "daily", "weekly"):
            raise Err(422, "validation_failed", "repeat is none, daily or weekly.")
        rem = body.get("reminder_minutes")
        if rem is not None and (not isinstance(rem, int) or not 0 <= rem <= 10080):
            raise Err(422, "validation_failed", "reminder_minutes is 0 to 10080.")
        attendees = []
        for ref in body.get("attendees") or []:
            u = self._user(str(ref))
            if u is None:
                raise Err(422, "principal_not_found", f"No account {ref!r} in this deployment.")
            attendees.append(u["id"])
        space_id = body.get("space_id")
        t = _now()
        with self.engine.begin() as conn:
            if space_id:
                self._readable(conn, space_id, actor)
            values = dict(title=title, starts_at=starts, ends_at=ends, timezone=tz, repeat=repeat,
                          repeat_until=body.get("repeat_until"), attendees_json=json.dumps(sorted(set(attendees))),
                          details=(body.get("details") or "")[:5000] or None, reminder_minutes=rem, space_id=space_id)
            if event_id is None:
                event_id = str(uuid.uuid4())
                conn.execute(events.insert().values(id=event_id, hub_id=self.hub, owner_id=actor.id, revision=1,
                                                    created_at=t, deleted_at=None, **values))
            else:
                e = self._event(conn, event_id, actor)
                if e.owner_id != actor.id:
                    raise Err(403, "permission_denied", "Only the event's organiser changes it.")
                if e.revision != body.get("expected_revision"):
                    raise Err(409, "revision_conflict", "The event changed. Reload it.", current_revision=e.revision)
                conn.execute(events.update().where(events.c.id == event_id).values(revision=e.revision + 1, **values))
                conn.execute(reminders.update().where(reminders.c.event_id == event_id, reminders.c.state == "pending")
                             .values(state="cancelled"))
            e = self._event(conn, event_id, actor)
            self._plan_reminders(conn, e)
            return self._event_json(e, actor)

    def _plan_reminders(self, conn, e, horizon: float = 14 * 86400) -> None:
        """Pending reminders for the next two weeks of occurrences (the loop tops them up)."""
        if e.reminder_minutes is None:
            return
        now = _now()
        people = {e.owner_id, *json.loads(e.attendees_json)}
        if e.space_id:
            people |= {r.user_id for r in conn.execute(sa.select(members.c.user_id).where(members.c.space_id == e.space_id))}
        for occ in self.occurrences(e, now, now + horizon):
            due = occ - e.reminder_minutes * 60
            if due < now - 60:
                continue
            for uid in people:
                exists = conn.execute(sa.select(reminders.c.id).where(
                    reminders.c.event_id == e.id, reminders.c.user_id == uid,
                    reminders.c.occurrence_at == occ)).first()
                if exists is None:
                    conn.execute(reminders.insert().values(id=str(uuid.uuid4()), event_id=e.id, user_id=uid,
                                                           occurrence_at=occ, due_at=due, state="pending",
                                                           sent_at=None))

    def list_events(self, actor, start: float, end: float) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(events).where(events.c.hub_id == self.hub, events.c.deleted_at.is_(None),
                                                        events.c.starts_at < end)).all()
            out = []
            for e in rows:
                if not self._may_see(conn, e, actor.id):
                    continue
                dur = e.ends_at - e.starts_at
                for occ in self.occurrences(e, start, end):
                    out.append({**self._event_json(e, actor), "occurrence_at": occ, "occurrence_end": occ + dur})
        return sorted(out, key=lambda x: x["occurrence_at"])

    def delete_event(self, actor, eid: str) -> None:
        with self.engine.begin() as conn:
            e = self._event(conn, eid, actor)
            if e.owner_id != actor.id:
                raise Err(403, "permission_denied", "Only the event's organiser deletes it.")
            conn.execute(events.update().where(events.c.id == eid).values(deleted_at=_now()))
            conn.execute(reminders.update().where(reminders.c.event_id == eid, reminders.c.state == "pending")
                         .values(state="cancelled"))

    def deliver_reminders(self, now: float | None = None) -> int:
        """Send due reminders once each (called by the bridge's loop). A
        reminder for someone who can no longer see the event is cancelled."""
        now = _now() if now is None else now
        sent = 0
        with self.engine.connect() as conn:
            due = conn.execute(sa.select(reminders).where(reminders.c.state == "pending",
                                                          reminders.c.due_at <= now)).all()
        for r in due:
            with self.engine.begin() as conn:
                e = conn.execute(sa.select(events).where(events.c.id == r.event_id)).first()
                if e is None or e.deleted_at is not None or not self._may_see(conn, e, r.user_id):
                    conn.execute(reminders.update().where(reminders.c.id == r.id).values(state="cancelled"))
                    continue
                claimed = conn.execute(reminders.update().where(reminders.c.id == r.id, reminders.c.state == "pending")
                                       .values(state="sent", sent_at=now)).rowcount
            if claimed:
                self.notify(r.user_id, "calendar.reminder", f"calendar.reminder:{r.id}", f"event:{r.event_id}:{int(r.occurrence_at)}")
                sent += 1
        # Top up the next occurrences of repeating events.
        with self.engine.begin() as conn:
            for e in conn.execute(sa.select(events).where(events.c.hub_id == self.hub, events.c.deleted_at.is_(None),
                                                          events.c.repeat != "none",
                                                          events.c.reminder_minutes.is_not(None))).all():
                self._plan_reminders(conn, e)
        return sent

    # -- notices ---------------------------------------------------------------------------------------
    def resolve_ref(self, ref: str, user_id: str) -> dict | None:
        """Title and link of a space or calendar notice, if this person may still open it."""
        kind, _, rest = ref.partition(":")
        with self.engine.connect() as conn:
            if kind == "space":
                sid, _, pid = rest.partition(":")
                try:
                    s = self._space(conn, sid)
                except Err:
                    return None
                if self._role(conn, s, type("A", (), {"id": user_id})()) is None:
                    return None
                return {"title": f"#{s.name}" if s.kind == "channel" else s.name, "link": f"/spaces/{sid}"}
            if kind == "event":
                eid, _, occ = rest.partition(":")
                e = conn.execute(sa.select(events).where(events.c.id == eid)).first()
                if e is None or e.deleted_at is not None or not self._may_see(conn, e, user_id):
                    return None
                when = datetime.fromtimestamp(int(occ or e.starts_at), ZoneInfo(e.timezone)).strftime("%a %d %b, %H:%M")
                return {"title": f"{e.title} · {when}", "link": "/spaces?tab=calendar"}
        return None


_services: dict[str, SpacesService] = {}


def service_for(hub_dir: Path, engine, hub: str) -> SpacesService:
    key = str(Path(hub_dir).resolve())
    svc = _services.get(key)
    if svc is None or svc.engine is not engine:
        svc = SpacesService(hub_dir, engine, hub)
        _services[key] = svc
    return svc
