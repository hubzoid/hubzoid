"""GitZoid's own data: one SQLite file in WAL mode, `gitzoid.db`, in the hub's
private data folder (`hub.data_dir` in workflows, `hubzoid.data_dir()` in chat
tools). Agent file tools cannot read that folder, and it is in the hub backup.

Every write is an upsert keyed by a natural key, because a workflow step can
run twice after a crash. Schema changes are numbered migrations below; a new
one is appended, never edited.
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = [
    # 1: the v1 schema
    """
    CREATE TABLE reviews (
        repo TEXT NOT NULL, number INTEGER NOT NULL,
        title TEXT, author TEXT, url TEXT,
        head_sha TEXT, reviewed_sha TEXT, state TEXT NOT NULL DEFAULT 'waiting',
        verdict TEXT, rules_fp TEXT, rules_version TEXT, view TEXT, summary_comment_id INTEGER,
        pushes INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT,
        status_posted TEXT, note TEXT, updated_at TEXT,
        PRIMARY KEY (repo, number));
    CREATE TABLE findings (
        id TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
        path TEXT, line INTEGER, function TEXT, anchor TEXT,
        category TEXT, rule TEXT, severity TEXT, confidence TEXT, body TEXT,
        state TEXT NOT NULL DEFAULT 'open', reason TEXT, by_user TEXT,
        first_sha TEXT, last_sha TEXT, comment_id INTEGER,
        created_at TEXT, updated_at TEXT);
    CREATE INDEX findings_pr ON findings (repo, number, state);
    CREATE TABLE finding_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL, at TEXT NOT NULL,
        event TEXT NOT NULL, sha TEXT, by_user TEXT, reason TEXT);
    CREATE INDEX finding_events_at ON finding_events (at);
    CREATE TABLE commands (
        key TEXT PRIMARY KEY, repo TEXT, number INTEGER, comment_id INTEGER,
        author TEXT, command TEXT, outcome TEXT, at TEXT);
    CREATE TABLE rule_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, paths TEXT, text TEXT NOT NULL,
        proposed_by TEXT, state TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at TEXT,
        note TEXT, file TEXT, created_at TEXT);
    CREATE TABLE security_ledger (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, repo TEXT NOT NULL,
        package TEXT, version TEXT, advisory TEXT, severity TEXT, kev INTEGER NOT NULL DEFAULT 0,
        title TEXT, impact TEXT, fix TEXT, path TEXT, line INTEGER,
        state TEXT NOT NULL DEFAULT 'open', first_seen TEXT, last_seen TEXT,
        alerted_at TEXT, alerted_severity TEXT, alerted_kev INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX ledger_repo ON security_ledger (repo, state);
    CREATE TABLE coverage (
        repo TEXT NOT NULL, check_name TEXT NOT NULL, last_ok TEXT, last_error TEXT,
        error_at TEXT, alerted_at TEXT, PRIMARY KEY (repo, check_name));
    CREATE TABLE audit_progress (
        repo TEXT PRIMARY KEY, sha TEXT, done TEXT NOT NULL DEFAULT '[]',
        queued TEXT NOT NULL DEFAULT '[]', last_full TEXT, started_at TEXT);
    CREATE TABLE profiles (
        repo TEXT PRIMARY KEY, sha TEXT, profile TEXT, built_at TEXT,
        stale INTEGER NOT NULL DEFAULT 0, stale_reason TEXT);
    CREATE TABLE cursors (key TEXT PRIMARY KEY, etag TEXT, value TEXT, updated_at TEXT);
    CREATE TABLE leases (name TEXT PRIMARY KEY, owner TEXT, expires REAL);
    CREATE TABLE usage (bucket TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bucket, kind));
    """,
    # 2: publication is separate from analysis; events survive later PR updates.
    """
    ALTER TABLE reviews ADD COLUMN publication_fp TEXT;
    ALTER TABLE reviews ADD COLUMN published_sha TEXT;
    CREATE TABLE review_runs (
        repo TEXT NOT NULL, number INTEGER NOT NULL, sha TEXT NOT NULL,
        fingerprint TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY (repo, number, sha, fingerprint));
    ALTER TABLE security_ledger ADD COLUMN assessment_fp TEXT;
    """,
    # 3: a committable one-line suggestion on a finding's inline comment.
    """
    ALTER TABLE findings ADD COLUMN suggestion TEXT;
    """,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _wal(con) -> None:
    """Switch the file to WAL (it stays that way). The switch doesn't wait for
    other connections the way statements do, so two first runs opening a new
    file at once retry it briefly."""
    for attempt in range(100):
        try:
            con.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 99:
                raise
            time.sleep(0.02 + random.random() * 0.05)


class Store:
    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "gitzoid.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            _wal(con)
            # Two first runs can start at once: read the version inside the
            # write transaction, so only one of them migrates.
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                for n, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                    for statement in (x.strip() for x in sql.split(";")):
                        if statement:
                            con.execute(statement)
                    con.execute(f"PRAGMA user_version = {n}")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
        finally:
            con.close()

    def _one(self, sql: str, *args) -> dict | None:
        with self.db() as con:
            row = con.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args) -> list[dict]:
        with self.db() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def _upsert(self, table: str, keys: dict, fields: dict) -> None:
        cols = {**keys, **fields}
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields) or f"{next(iter(keys))} = excluded.{next(iter(keys))}"
        with self.db() as con:
            con.execute(f"INSERT INTO {table} ({names}) VALUES ({marks}) "
                        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}", tuple(cols.values()))

    # ---------------------------------------------------------------- reviews

    def review(self, repo: str, number: int) -> dict | None:
        return self._one("SELECT * FROM reviews WHERE repo = ? AND number = ?", repo, number)

    def save_review(self, repo: str, number: int, **fields) -> None:
        if "view" in fields and not isinstance(fields["view"], (str, type(None))):
            fields["view"] = json.dumps(fields["view"])
        self._upsert("reviews", {"repo": repo, "number": number}, {**fields, "updated_at": now_iso()})

    def record_review(self, repo: str, number: int, sha: str, fingerprint: str) -> None:
        with self.db() as con:
            con.execute("INSERT OR IGNORE INTO review_runs VALUES (?, ?, ?, ?, ?)",
                        (repo, number, sha, fingerprint, now_iso()))

    def review_runs(self, start: str, end: str, repos: list[str]) -> list[dict]:
        return [r for r in self._all("SELECT * FROM review_runs WHERE at >= ? AND at < ?", start, end)
                if r["repo"] in repos]

    def view(self, repo: str, number: int) -> dict | None:
        row = self.review(repo, number)
        return json.loads(row["view"]) if row and row.get("view") else None

    def reviews(self, repo: str | None = None) -> list[dict]:
        if repo:
            return self._all("SELECT * FROM reviews WHERE repo = ? ORDER BY number", repo)
        return self._all("SELECT * FROM reviews ORDER BY repo, number")

    # --------------------------------------------------------------- findings

    def finding(self, fid: str) -> dict | None:
        return self._one("SELECT * FROM findings WHERE id = ?", fid)

    def finding_by_comment(self, repo: str, comment_id: int) -> dict | None:
        return self._one("SELECT * FROM findings WHERE repo = ? AND comment_id = ?", repo, comment_id)

    def findings_for(self, repo: str, number: int, states: tuple[str, ...] | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM findings WHERE repo = ? AND number = ? ORDER BY created_at, id", repo, number)
        return [r for r in rows if states is None or r["state"] in states]

    def search_findings(self, repo: str | None = None, state: str | None = None, rule: str | None = None,
                        limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM findings WHERE 1=1", []
        if repo:
            sql, args = sql + " AND repo = ?", [*args, repo]
        if state:
            sql, args = sql + " AND state = ?", [*args, state]
        if rule:
            sql, args = sql + " AND lower(rule) LIKE ?", [*args, f"%{rule.lower()}%"]
        return self._all(sql + " ORDER BY updated_at DESC LIMIT ?", *args, limit)

    def save_finding(self, f: dict, event: str | None = None, sha: str | None = None,
                     by_user: str | None = None, reason: str | None = None) -> None:
        stamp = now_iso()
        fields = {k: f.get(k) for k in ("repo", "number", "path", "line", "function", "anchor", "category",
                                         "rule", "severity", "confidence", "body", "suggestion", "state", "reason",
                                         "by_user", "first_sha", "last_sha", "comment_id", "created_at")}
        fields["created_at"] = fields["created_at"] or stamp
        self._upsert("findings", {"id": f["id"]}, {**fields, "updated_at": stamp})
        if event:
            with self.db() as con:
                con.execute("INSERT INTO finding_events (finding_id, at, event, sha, by_user, reason) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (f["id"], stamp, event, sha, by_user, reason))

    def finding_events(self, since: str, repos: list[str] | None = None) -> list[dict]:
        rows = self._all("SELECT e.*, f.repo, f.rule, f.category, f.severity FROM finding_events e "
                         "JOIN findings f ON f.id = e.finding_id WHERE e.at >= ? ORDER BY e.at", since)
        return [r for r in rows if repos is None or r["repo"] in repos]

    # --------------------------------------------------------------- commands

    def claim_command(self, key: str, **fields) -> str:
        """"new" the first time a command key is seen, "pending" when an earlier
        attempt stopped before finishing (its effects are repeatable, so it runs
        again), "done" when it finished: a redelivery then does nothing."""
        with self.db() as con:
            cur = con.execute("INSERT OR IGNORE INTO commands (key, repo, number, comment_id, author, command, at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (key, fields.get("repo"), fields.get("number"), fields.get("comment_id"),
                               fields.get("author"), fields.get("command"), now_iso()))
            if cur.rowcount == 1:
                return "new"
            row = con.execute("SELECT outcome FROM commands WHERE key = ?", (key,)).fetchone()
            return "pending" if row["outcome"] is None else "done"

    def finish_command(self, key: str, outcome: str) -> None:
        with self.db() as con:
            con.execute("UPDATE commands SET outcome = ? WHERE key = ?", (outcome, key))

    def command(self, key: str) -> dict | None:
        return self._one("SELECT * FROM commands WHERE key = ?", key)

    # ------------------------------------------------------------ rule drafts

    def add_draft(self, title: str, text: str, paths: list[str], proposed_by: str | None) -> int:
        with self.db() as con:
            cur = con.execute("INSERT INTO rule_drafts (title, paths, text, proposed_by, created_at) "
                              "VALUES (?, ?, ?, ?, ?)", (title, json.dumps(paths), text, proposed_by, now_iso()))
            return int(cur.lastrowid)

    def drafts(self, state: str = "pending") -> list[dict]:
        rows = self._all("SELECT * FROM rule_drafts WHERE state = ? ORDER BY id", state)
        return [{**r, "paths": json.loads(r["paths"] or "[]")} for r in rows]

    def draft(self, draft_id: int) -> dict | None:
        r = self._one("SELECT * FROM rule_drafts WHERE id = ?", draft_id)
        return {**r, "paths": json.loads(r["paths"] or "[]")} if r else None

    def decide_draft(self, draft_id: int, state: str, by: str | None, note: str = "", file: str | None = None) -> None:
        with self.db() as con:
            con.execute("UPDATE rule_drafts SET state = ?, decided_by = ?, decided_at = ?, note = ?, file = ? "
                        "WHERE id = ?", (state, by, now_iso(), note, file, draft_id))

    # ----------------------------------------------------------------- ledger

    def ledger(self, key: str) -> dict | None:
        return self._one("SELECT * FROM security_ledger WHERE key = ?", key)

    def save_ledger(self, key: str, **fields) -> None:
        """Update the fields given on an existing entry, or create a full one."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db() as con:
            cur = con.execute(f"UPDATE security_ledger SET {sets} WHERE key = ?", (*fields.values(), key))
            if cur.rowcount:
                return
        self._upsert("security_ledger", {"key": key}, fields)

    def unalert(self, keys: list[str]) -> None:
        """The alert for these entries was not delivered: the next run alerts again."""
        with self.db() as con:
            con.executemany("UPDATE security_ledger SET alerted_at = NULL, alerted_severity = NULL, alerted_kev = 0 "
                            "WHERE key = ?", [(k,) for k in keys])

    def ledger_rows(self, repos: list[str] | None = None, state: str | None = "open",
                    kind: str | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM security_ledger ORDER BY repo, severity, key")
        return [r for r in rows if (repos is None or r["repo"] in repos)
                and (state is None or r["state"] == state) and (kind is None or r["kind"] == kind)]

    # --------------------------------------------------------------- coverage

    def coverage_ok(self, repo: str, check: str) -> None:
        self._upsert("coverage", {"repo": repo, "check_name": check},
                     {"last_ok": now_iso(), "last_error": None, "error_at": None, "alerted_at": None})

    def coverage_error(self, repo: str, check: str, error: str) -> None:
        self._upsert("coverage", {"repo": repo, "check_name": check}, {"last_error": error[:500], "error_at": now_iso()})

    def coverage_alerted(self, repo: str, check: str) -> None:
        with self.db() as con:
            con.execute("UPDATE coverage SET alerted_at = ? WHERE repo = ? AND check_name = ?", (now_iso(), repo, check))

    def coverage(self, repos: list[str] | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM coverage ORDER BY repo, check_name")
        return [r for r in rows if repos is None or r["repo"] in repos]

    # ------------------------------------------------------------------ audit

    def audit(self, repo: str) -> dict:
        r = self._one("SELECT * FROM audit_progress WHERE repo = ?", repo)
        if not r:
            return {"repo": repo, "sha": None, "done": [], "queued": [], "last_full": None, "started_at": None}
        return {**r, "done": json.loads(r["done"]), "queued": json.loads(r["queued"])}

    def save_audit(self, repo: str, **fields) -> None:
        for k in ("done", "queued"):
            if k in fields:
                fields[k] = json.dumps(sorted(set(fields[k])))
        self._upsert("audit_progress", {"repo": repo}, fields)

    def queue_audit(self, repo: str, paths: list[str]) -> None:
        if paths:
            state = self.audit(repo)
            self.save_audit(repo, queued=[*state["queued"], *paths])

    # --------------------------------------------------------------- profiles

    def profile(self, repo: str) -> dict | None:
        r = self._one("SELECT * FROM profiles WHERE repo = ?", repo)
        return {**r, "profile": json.loads(r["profile"] or "{}")} if r else None

    def save_profile(self, repo: str, sha: str, profile: dict) -> None:
        self._upsert("profiles", {"repo": repo}, {"sha": sha, "profile": json.dumps(profile),
                                                   "built_at": now_iso(), "stale": 0, "stale_reason": None})

    def mark_profile_stale(self, repo: str, reason: str) -> None:
        with self.db() as con:
            con.execute("UPDATE profiles SET stale = 1, stale_reason = ? WHERE repo = ?", (reason[:300], repo))

    # ---------------------------------------------------------------- cursors

    def cursor(self, key: str) -> dict | None:
        r = self._one("SELECT * FROM cursors WHERE key = ?", key)
        return {**r, "value": json.loads(r["value"]) if r and r["value"] else None} if r else None

    def save_cursor(self, key: str, value=None, etag: str | None = None) -> None:
        self._upsert("cursors", {"key": key}, {"etag": etag, "value": json.dumps(value), "updated_at": now_iso()})

    # ----------------------------------------------------------------- leases

    def acquire(self, name: str, owner: str, ttl: float = 900) -> bool:
        """One holder per name until it releases or the lease expires. Reviews
        and commands on one repo take turns through this."""
        stamp = time.time()
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT owner, expires FROM leases WHERE name = ?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires"] > stamp:
                con.execute("ROLLBACK")
                return False
            con.execute("INSERT INTO leases (name, owner, expires) VALUES (?, ?, ?) "
                        "ON CONFLICT (name) DO UPDATE SET owner = excluded.owner, expires = excluded.expires",
                        (name, owner, stamp + ttl))
            con.execute("COMMIT")
            return True

    def release(self, name: str, owner: str) -> None:
        with self.db() as con:
            con.execute("DELETE FROM leases WHERE name = ? AND owner = ?", (name, owner))

    # ------------------------------------------------------------------ usage

    def take(self, kind: str, bucket: str, limit: int, n: int = 1) -> bool:
        """Count `n` uses of `kind` in `bucket` (a day or an hour) if that stays
        within `limit`. False means the cap is reached and nothing was counted."""
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT n FROM usage WHERE bucket = ? AND kind = ?", (bucket, kind)).fetchone()
            used = row["n"] if row else 0
            if used + n > limit:
                con.execute("ROLLBACK")
                return False
            con.execute("INSERT INTO usage (bucket, kind, n) VALUES (?, ?, ?) "
                        "ON CONFLICT (bucket, kind) DO UPDATE SET n = n + excluded.n", (bucket, kind, n))
            con.execute("COMMIT")
            return True

    def used(self, kind: str, bucket: str) -> int:
        r = self._one("SELECT n FROM usage WHERE bucket = ? AND kind = ?", bucket, kind)
        return r["n"] if r else 0
