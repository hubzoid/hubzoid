"""Readiness to move a deployment off Open WebUI (HUB-85, prepared part).

``hubzoid migrate readiness`` reads an Open WebUI database **read-only** (the
same engine as ``hubzoid migrate openwebui``: SQLite opened ``mode=ro``,
PostgreSQL in a read-only repeatable-read transaction) and reports, per
category, how many objects there are, where each belongs in Hubzoid 2.0, and
whether this build can carry it:

* ``ready``: an importer exists today (``hubzoid migrate openwebui``),
* ``prepared``: the native place exists in this build; its importer is not written yet,
* ``decision``: someone must choose how it maps (it can't be guessed),
* ``blocked``: there is no native place yet, or it must stay dormant.

It reads counts and safe aggregates only: no message text, no names, no
emails, no secrets. Nothing is written or migrated, and the report says so. The
mapping matrix (M01 to M13) shows which checks this build can already run. A
digest of the inventory lets a later plan prove it is about the same source.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect, text

from .control import canonical

#: (key, label, table, target in 2.0, status, note)
CATEGORIES = [
    ("accounts", "Accounts", "user", "Native sign-in and accounts", "ready",
     "Imported by `hubzoid migrate openwebui`; supported password hashes carry over."),
    ("oauth_accounts", "Single sign-on accounts", "user", "Native sign-in (issuer and subject)", "ready",
     "Bound by issuer and subject, never merged by email."),
    ("pending_accounts", "Pending accounts", "user", "Native sign-in", "decision",
     "Approve or remove before moving; pending people get no access either way."),
    ("groups", "Groups", "group", "Access groups (HUB-39, HUB-87)", "decision",
     "2.0 grants access per person; each group needs a decision: its members' grants, or a directory group."),
    ("chats", "Conversations", "chat", "One chat engine (HUB-72)", "ready",
     "Branches, selected heads and per-message models are kept."),
    ("archived_chats", "Archived conversations", "chat", "Organisation (HUB-73)", "ready", ""),
    ("pinned_chats", "Pinned conversations", "chat", "Organisation: pins (HUB-73)", "prepared",
     "Pins exist natively; today's importer records them as lost."),
    ("folders", "Folders", "folder", "Organisation: folders (HUB-73)", "prepared",
     "Nested folders and defaults exist natively; today's importer flattens them."),
    ("tags", "Tags", "tag", "Organisation: tags (HUB-73)", "prepared", ""),
    ("feedback", "Ratings", "feedback", "Feedback (HUB-76)", "prepared",
     "Native ratings keep author and revision; original values must stay recoverable."),
    ("files", "Files", "file", "Files with lasting identity (HUB-74)", "ready",
     "Uploads are copied and hashed; sizes over today's limit stay readable."),
    ("shares", "Shared links", "shared_chat", "Snapshot links (HUB-79)", "prepared",
     "Native links keep their audience; links that can't keep it safely must be reissued with notice."),
    ("models", "Custom models and agents", "model", "Agents and presets (HUB-78, HUB-81)", "decision",
     "Each becomes an agent folder, a preset, or a read-only historical name. Never a different model."),
    ("prompts", "Prompts", "prompt", "Prompts with variables (HUB-81)", "prepared",
     "Shared prompts become hub files (reviewed); personal ones stay personal."),
    ("skills", "Skills", "skill", "Skills (HUB-81)", "prepared", "Imported as reviewed drafts of hub skill files."),
    ("tools", "Tools", "tool", "Tool extensions (HUB-81)", "blocked",
     "Executable code. It may only be imported dormant, pending reviewed activation (not in this build)."),
    ("functions", "Functions, filters and pipes", "function", "Tool extensions (HUB-81)", "blocked",
     "Executable code, imported dormant only; activation needs the reviewed publication workflow."),
    ("knowledge", "Knowledge bases", "knowledge", "Sources and collections (HUB-75)", "prepared",
     "Collections, extraction and citations exist natively; vector indexes are not carried (rebuilt)."),
    ("memories", "Memories", "memory", "Exported to a file with a disposition report", "decision",
     "2.0 has no memory system; memories are exported, not activated."),
    ("notes", "Notes", "note", "Notes (HUB-80)", "prepared", ""),
    ("channels", "Channels", "channel", "Channels (HUB-80)", "prepared",
     "Channel apps and webhooks stay disabled until reviewed."),
    ("channel_messages", "Channel messages", "message", "Channels (HUB-80)", "prepared", ""),
    ("channel_webhooks", "Channel webhooks", "channel_webhook", "Channels (HUB-80)", "blocked",
     "Imported disabled; each needs a reviewed native activation."),
    ("calendars", "Calendars", "calendar", "Calendar (HUB-80)", "prepared", ""),
    ("calendar_events", "Calendar events", "calendar_event", "Calendar (HUB-80)", "prepared",
     "Recurrence exceptions beyond daily/weekly need a decision."),
    ("automations", "Automations", "automation", "Workflows and schedules", "blocked",
     "Schedules import disabled; each needs reviewed activation."),
    ("api_keys", "API keys", "api_key", "Reissued as scoped native credentials (HUB-86)", "blocked",
     "Old keys never work natively. Owners reissue them."),
    ("oauth_sessions", "Connected-account sessions", "oauth_session", "Native connections (reconnect)", "decision",
     "Each person reconnects their accounts once; tokens are not copied."),
]

#: The required mapping matrix, and what this build can show for each.
MATRIX = [
    ("M01", "Identity", "partial", "Accounts, issuer/subject and password hashes import today; MFA and SCIM wait."),
    ("M02", "Permissions", "unrun", "Effective-permission comparison needs the HUB-39 access model."),
    ("M03", "Organisation and feedback", "prepared", "Native folders, tags, pins and ratings exist; importers not written."),
    ("M04", "Content and models", "partial", "Graph, heads and model provenance import today; unserved models need names."),
    ("M05", "Files", "partial", "Bytes are copied and hashed today; knowledge assets wait for M07."),
    ("M06", "Sharing", "prepared", "Native audiences exist; today's importer disables restricted links."),
    ("M07", "Workspace", "prepared", "Prompts, skills, knowledge, notes, channels and calendar have native places; "
                                     "code, schedules and webhooks stay dormant."),
    ("M08", "Clients", "unrun", "Session expiry and credential reissue need HUB-86."),
    ("M09", "Verification", "partial", "Dispositions and hashes per object in today's importer report."),
    ("M10", "Rehearsal", "partial", "SQLite single-hub rehearsal on a copy works; gateway and PostgreSQL wait."),
    ("M11", "Restore", "unrun", "Needs the HUB-109 backup package."),
    ("M12", "Cutover", "unrun", "Needs every row above."),
    ("M13", "Removed option", "unrun", "Open WebUI stays installable until the migration is complete and approved."),
]


@dataclass
class Readiness:
    source: dict = field(default_factory=dict)
    categories: list[dict] = field(default_factory=list)
    matrix: list[dict] = field(default_factory=list)
    digest: str = ""
    totals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"version": 1, "kind": "hubzoid-migration-readiness", "writes": "none", "source": self.source,
                "digest": self.digest, "totals": self.totals, "categories": self.categories, "matrix": self.matrix,
                "verdict": self.verdict()}

    def verdict(self) -> str:
        if any(c["status"] == "blocked" and c["count"] for c in self.categories):
            return "blocked"
        if any(c["status"] in ("decision", "prepared") and c["count"] for c in self.categories):
            return "not_ready"
        return "ready_to_rehearse"


def _count(conn, tables: set[str], table: str, where: str = "") -> int | None:
    if table not in tables:
        return None
    return int(conn.execute(text(f'SELECT COUNT(*) FROM "{table}" {where}')).scalar() or 0)


def inventory(engine) -> Readiness:
    """Counts and safe aggregates of an Open WebUI database (read-only)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if not {"user", "chat"} <= tables:
        raise ValueError("This does not look like an Open WebUI database (no user or chat table).")
    cols = {t: {c["name"] for c in insp.get_columns(t)} for t in ("user", "chat", "file", "shared_chat", "function")
            if t in tables}
    out = Readiness()
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() \
            if "alembic_version" in tables else None
        counts: dict[str, int | None] = {}
        counts["accounts"] = _count(conn, tables, "user")
        counts["pending_accounts"] = _count(conn, tables, "user", "WHERE role = 'pending'") \
            if "role" in cols.get("user", set()) else None
        if "oauth_sub" in cols.get("user", set()):
            counts["oauth_accounts"] = _count(conn, tables, "user", "WHERE oauth_sub IS NOT NULL AND oauth_sub != ''")
        elif "oauth" in cols.get("user", set()):
            counts["oauth_accounts"] = _count(conn, tables, "user",
                                              "WHERE oauth IS NOT NULL AND CAST(oauth AS TEXT) NOT IN ('null', '{}', '')")
        else:
            counts["oauth_accounts"] = None
        counts["groups"] = _count(conn, tables, "group")
        counts["chats"] = _count(conn, tables, "chat")
        counts["archived_chats"] = _count(conn, tables, "chat", "WHERE archived = 1") \
            if "archived" in cols.get("chat", set()) else None
        counts["pinned_chats"] = _count(conn, tables, "chat", "WHERE pinned = 1") \
            if "pinned" in cols.get("chat", set()) else None
        for key, _label, table, *_ in CATEGORIES:
            if key not in counts:
                counts[key] = _count(conn, tables, table)
        file_bytes = None
        if "file" in tables and "meta" in cols.get("file", set()):
            file_bytes = 0
            for (meta,) in conn.execute(text('SELECT meta FROM "file"')):
                try:
                    m = json.loads(meta) if isinstance(meta, str) else (meta or {})
                    file_bytes += int(m.get("size") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
        executable = {}
        if "function" in tables and "type" in cols.get("function", set()):
            for kind, n in conn.execute(text('SELECT type, COUNT(*) FROM "function" GROUP BY type')):
                executable[str(kind)] = int(n)
        roles = {}
        if "role" in cols.get("user", set()):
            for role, n in conn.execute(text('SELECT role, COUNT(*) FROM "user" GROUP BY role')):
                roles[str(role)] = int(n)
    for key, label, table, target, status, note in CATEGORIES:
        n = counts.get(key)
        out.categories.append({"key": key, "label": label, "count": n, "target": target,
                               "status": status if n else ("absent" if n is None else "empty"), "note": note,
                               **({"bytes": file_bytes} if key == "files" and file_bytes is not None else {}),
                               **({"by_type": executable} if key == "functions" and executable else {}),
                               **({"by_role": roles} if key == "accounts" and roles else {})})
    out.matrix = [{"id": i, "area": a, "status": s, "note": n} for i, a, s, n in MATRIX]
    out.source = {"engine": engine.dialect.name, "schema_revision": version, "tables": len(tables),
                  "unknown_tables": sorted(tables - {c[2] for c in CATEGORIES} - {
                      "auth", "alembic_version", "config", "config_old", "chat_message", "chat_file", "chatidtag",
                      "group_member", "access_grant", "channel_member", "channel_file", "message_reaction",
                      "knowledge_file", "knowledge_directory", "calendar_event_attendee", "automation_run",
                      "document", "prompt_history", "pinned_note"})}
    out.totals = {s: sum(1 for c in out.categories if c["status"] == s) for s in
                  ("ready", "prepared", "decision", "blocked", "empty", "absent")}
    out.digest = canonical.digest({"source": out.source, "counts": {c["key"]: c["count"] for c in out.categories}})
    return out


def run(path: Path, *, owui_db: str | None = None) -> Readiness:
    from .migrate_openwebui import _source_engine, locate

    setup = locate(path, owui_db=owui_db)
    engine = _source_engine(setup.source_url, setup.source_schema)
    try:
        return inventory(engine)
    finally:
        engine.dispose()


def to_markdown(r: Readiness) -> str:
    d = r.to_dict()
    lines = ["# Moving off Open WebUI: readiness", "",
             f"Nothing was written. Source: {d['source']['engine']}, schema {d['source']['schema_revision'] or 'unknown'}.",
             f"Verdict: **{d['verdict'].replace('_', ' ')}**. Inventory digest `{d['digest']}`.", "",
             "| Category | Count | Goes to | Status | Note |", "|---|---:|---|---|---|"]
    for c in d["categories"]:
        if c["status"] == "absent":
            continue
        lines.append(f"| {c['label']} | {c['count']} | {c['target']} | {c['status']} | {c['note']} |")
    lines += ["", "| Check | Area | This build | Note |", "|---|---|---|---|"]
    lines += [f"| {m['id']} | {m['area']} | {m['status']} | {m['note']} |" for m in d["matrix"]]
    if d["source"]["unknown_tables"]:
        lines += ["", "Tables this report doesn't know yet: " + ", ".join(d["source"]["unknown_tables"]) + "."]
    return "\n".join(lines) + "\n"


def register(migrate_app) -> None:
    import typer

    from .control import cli_common as cc

    @migrate_app.command("readiness")
    def readiness(
        path: Path = typer.Argument(Path("."), help="Hub folder, hub registered in a gateway, or gateway data folder."),
        owui_db: str = typer.Option(None, "--owui-db", help="Open WebUI database URL (SQLAlchemy) to read instead."),
        output: Path = typer.Option(None, "--output", help="Also write the report as Markdown here."),
        as_json: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    ) -> None:
        """What moving this deployment off Open WebUI would take. Reads only; writes nothing."""
        from .migrate_openwebui import MigrationBlocked

        try:
            r = run(path, owui_db=owui_db)
        except (MigrationBlocked, ValueError) as exc:
            cc.fail(422, "migration_blocked", str(exc), as_json)
        if output is not None:
            output.write_text(to_markdown(r), encoding="utf-8")
        if as_json:
            cc.emit(r.to_dict(), True)
        else:
            typer.echo(to_markdown(r))
        raise typer.Exit(0 if r.verdict() == "ready_to_rehearse" else 6)
