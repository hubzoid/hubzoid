"""Simple file-system memory: append-only JSONL per scope.

v1: filesystem only. Scope keys:
  user      -> <hub>/output/_memory/user.jsonl
  session   -> <hub>/output/_memory/session_<session_id>.jsonl
  agent     -> <hub>/output/_memory/agent.jsonl
  hub       -> <hub>/output/_memory/hub.jsonl

Mem0 / Zep / Postgres-backed adapters are deferred to v1.1.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from agents import function_tool


def make(ctx) -> list:
    memory_dir: Path = ctx.output_dir / "_memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    session_id = ctx.session_id

    def _path(scope: str) -> Path:
        scope = scope.lower().strip()
        name = {
            "user": "user.jsonl",
            "session": f"session_{session_id}.jsonl",
            "agent": "agent.jsonl",
            "hub": "hub.jsonl",
            "customer": "hub.jsonl",  # legacy alias from blueprint
        }.get(scope, f"{scope}.jsonl")
        return memory_dir / name

    @function_tool
    def remember(fact: str, scope: str = "session") -> str:
        """Save a fact to durable memory.

        Args:
            fact: One-sentence fact, e.g. "the user prefers terse replies".
            scope: One of user | session | agent | hub. Default: session.

        Returns:
            The memory entry id (use with forget()).
        """
        entry_id = uuid.uuid4().hex[:12]
        record = {
            "id": entry_id,
            "ts": int(time.time()),
            "scope": scope,
            "fact": fact,
        }
        with _path(scope).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return entry_id

    @function_tool
    def recall(query: str, scope: str = "session", limit: int = 20) -> str:
        """Look up facts saved with remember().

        Args:
            query: Substring to match (case-insensitive). Empty string returns the newest entries.
            scope: One of user | session | agent | hub. Default: session.
            limit: Max entries to return. Default: 20.

        Returns:
            Newline-separated `id  fact` rows, newest first.
        """
        p = _path(scope)
        if not p.is_file():
            return "(nothing remembered yet)"
        rows: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        q = (query or "").lower()
        if q:
            rows = [r for r in rows if q in r.get("fact", "").lower()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        if not rows:
            return "(no matching memories)"
        return "\n".join(f"{r['id']}  {r['fact']}" for r in rows[:limit])

    @function_tool
    def forget(entry_id: str, scope: str = "session") -> str:
        """Delete a remembered fact by id.

        Args:
            entry_id: The id returned by remember().
            scope: The scope you saved it under. Default: session.

        Returns:
            "ok" on success, error message otherwise.
        """
        p = _path(scope)
        if not p.is_file():
            return "[forget: nothing to forget]"
        kept: list[str] = []
        removed = False
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if rec.get("id") == entry_id:
                removed = True
                continue
            kept.append(line)
        if not removed:
            return f"[forget: id {entry_id!r} not found]"
        p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return "ok"

    return [remember, recall, forget]
