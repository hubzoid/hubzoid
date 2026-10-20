"""Readiness to move a deployment off Open WebUI (HUB-85, prepared part).

``hubzoid migrate readiness`` reads an Open WebUI database **read-only** (the
same engine as ``hubzoid migrate openwebui``: SQLite opened ``mode=ro``,
PostgreSQL in a read-only repeatable-read transaction) and reports, per
category, how many objects there are, where each belongs in Hubzoid 2.0, and
whether 2.0 carries it:

* ``ready``: ``hubzoid migrate openwebui`` imports it,
* ``prepared``: 2.0 has a place for it, but the migration doesn't fill it,
* ``decision``: someone must choose how it maps (it can't be guessed),
* ``blocked``: there is no native place yet, or it must stay dormant.

It reads counts and safe aggregates only: no message text, no names, no
emails, no secrets. Nothing is written or migrated, and the report says so. The
mapping matrix (M01 to M13) says where 2.0 stands on each area. A
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
    ("accounts", "Accounts", "user", "Hubzoid accounts", "ready",
     "Imported by `hubzoid migrate openwebui`; supported password hashes carry over."),
    ("oauth_accounts", "Single sign-on accounts", "user", "Hubzoid sign-in (issuer and subject)", "ready",
     "Bound by issuer and subject, never merged by email."),
    ("pending_accounts", "Pending accounts", "user", "Hubzoid accounts", "decision",
     "Approve or remove before moving; pending people get no access either way."),
    ("groups", "Groups", "group", "Access per person", "decision",
     "Groups give no access in 2.0. Grant each group's members access in Settings → People & access."),
    ("chats", "Conversations", "chat", "Chats", "ready",
     "Branches, the selected branch and each message's model are kept."),
    ("archived_chats", "Archived conversations", "chat", "Archived chats", "ready", ""),
    ("pinned_chats", "Pinned conversations", "chat", "Chats", "prepared",
     "Imported as normal chats; people pin them again."),
    ("folders", "Folders", "folder", "Chats", "prepared",
     "Imported without their folders; people file them again."),
    ("tags", "Tags", "tag", "Not imported", "prepared", ""),
    ("feedback", "Ratings", "feedback", "Not imported", "prepared", ""),
    ("files", "Files", "file", "Chat attachments", "ready",
     "Uploads are copied and hashed; files over the size limit stay readable."),
    ("shares", "Shared links", "shared_chat", "Shared links", "prepared",
     "Links for everyone signed in keep working; private and restricted links stay off until their "
     "owner shares again."),
    ("models", "Custom models and agents", "model", "Agents", "decision",
     "Chats of a model that isn't an agent here need --model-alias OLD=AGENT, or they are left out."),
    ("prompts", "Prompts", "prompt", "Not imported", "prepared",
     "Copy shared prompts into the agent's files (Settings → Agent instructions)."),
    ("skills", "Skills", "skill", "Not imported", "prepared",
     "Copy them into the agent's skills/ folder."),
    ("tools", "Tools", "tool", "Not imported", "blocked",
     "Executable code is never imported. Rebuild a tool as an agent tool or a connector."),
    ("functions", "Functions, filters and pipes", "function", "Not imported", "blocked",
     "Executable code is never imported. Rebuild what you need as agent tools or workflows."),
    ("knowledge", "Knowledge bases", "knowledge", "Not imported", "prepared",
     "Add the files to a source collection (Settings → Sources) and sync it."),
    ("memories", "Memories", "memory", "Not imported", "decision",
     "2.0 keeps one note per person per agent, saved only when the person asks. People add what "
     "they still need in Settings → Memory."),
    ("notes", "Notes", "note", "Not carried", "decision",
     "2.0 has no notes; copy them out before moving."),
    ("channels", "Channels", "channel", "Not carried", "decision",
     "2.0 has no channels; copy what matters out before moving."),
    ("channel_messages", "Channel messages", "message", "Not carried", "decision", ""),
    ("channel_webhooks", "Channel webhooks", "channel_webhook", "Not carried", "blocked",
     "Each webhook needs a replacement, such as a workflow."),
    ("calendars", "Calendars", "calendar", "Not carried", "decision",
     "2.0 has no calendar; copy it out before moving."),
    ("calendar_events", "Calendar events", "calendar_event", "Not carried", "decision", ""),
    ("automations", "Automations", "automation", "Workflows and schedules", "blocked",
     "Not imported. Recreate each as a schedule or workflow."),
    ("api_keys", "API keys", "api_key", "Personal API keys", "blocked",
     "Old keys stop working. People make personal keys in Settings → Security."),
    ("oauth_sessions", "Connected-account sessions", "oauth_session", "Connections", "decision",
     "Each person connects their accounts again once; tokens are not copied."),
]

#: The required mapping matrix, and where 2.0 stands on each.
MATRIX = [
    ("M01", "Identity", "ready", "Accounts, sign-in links and password hashes import. Open WebUI has no "
                                 "two-step sign-in to carry; administrators set it up at first sign-in."),
    ("M02", "Permissions", "ready", "Access comes from grants only. The migration's dry run counts the "
                                    "people who would have no agent access."),
    ("M03", "Organisation and feedback", "partial", "Pins, folders, tags and ratings are not imported."),
    ("M04", "Content and models", "ready", "Branches, the selected branch and each message's model "
                                           "import; chats of other models need --model-alias."),
    ("M05", "Files", "ready", "Attachments are copied and hashed. Knowledge bases are added again as sources."),
    ("M06", "Sharing", "partial", "Links for everyone signed in keep working; private and restricted ones stay off."),
    ("M07", "Workspace", "partial", "Prompts, skills and knowledge are copied by hand; notes, channels and "
                                    "calendar are not carried; code, schedules and webhooks are rebuilt."),
    ("M08", "Clients", "ready", "Open WebUI sessions end and people sign in again; old API keys are "
                                "replaced by personal keys."),
    ("M09", "Verification", "ready", "The migration reports what moved, what was left out and why, per object."),
    ("M10", "Rehearsal", "ready", "`hubzoid migrate openwebui --rehearse <empty folder>` migrates a copy of "
                                  "the hub, or of every hub of a gateway (SQLite). For PostgreSQL, restore "
                                  "dumps into scratch databases."),
    ("M11", "Restore", "ready", "Take `hubzoid backup` first. The migration only reads Open WebUI's "
                                "database, so 1.x can start again from it."),
    ("M12", "Cutover", "partial", "Stop the deployment, run --apply, then start 2.0. Decide the rows above first."),
    ("M13", "Removed option", "ready", "Hubzoid 2.0 has no Open WebUI mode; until the move is done, stay on 1.x."),
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
    verdict = {"blocked": "something here can't move as it is (see blocked rows)",
               "not_ready": "decide the rows marked decision or prepared first",
               "ready_to_rehearse": "ready to rehearse on a copy"}[d["verdict"]]
    lines = ["# Moving off Open WebUI: readiness", "",
             f"Nothing was written. Source: {d['source']['engine']}.",
             f"Verdict: **{verdict}**.", "",
             "| Category | Count | Goes to | Status | Note |", "|---|---:|---|---|---|"]
    for c in d["categories"]:
        if c["status"] == "absent":
            continue
        lines.append(f"| {c['label']} | {c['count']} | {c['target']} | {c['status']} | {c['note']} |")
    lines += ["", "| Area | Status | Note |", "|---|---|---|"]
    lines += [f"| {m['area']} | {m['status']} | {m['note']} |" for m in d["matrix"]]
    if d["source"]["unknown_tables"]:
        lines += ["", "Tables this report doesn't know yet: " + ", ".join(d["source"]["unknown_tables"]) + "."]
    lines += ["", f"Report id `{d['digest']}` (the same Open WebUI data gives the same id)."]
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
