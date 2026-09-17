"""Definition inspection without execution; execution history through DBOS API."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from .schedule_grammar import next_after
from .. import db

# A live dispatcher writes a heartbeat every ~60s. If the newest heartbeat is
# older than this while the deployment still marks schedules enabled, the
# dispatcher process is almost certainly gone (bridge crash/restart) even though
# the persisted health still says enabled. We surface that as 'stale' so the
# operator sees a stopped scheduler instead of a falsely healthy one.
STALE_AFTER_SECONDS = 150


def _stale(heartbeat: str | None, now: datetime | None = None) -> bool:
    if not heartbeat:
        return True
    try:
        beat = datetime.fromisoformat(heartbeat)
    except (TypeError, ValueError):
        return True
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - beat).total_seconds() > STALE_AFTER_SECONDS


def definitions(hub_dir) -> list[dict]:
    from .._fs import resolve_bucket

    root = resolve_bucket(Path(hub_dir), "workflows")
    if not root:
        return []
    rows = []
    seen = set()
    for file in sorted(root.glob("*/*.py")):
        if file.name.startswith("_"):
            continue
        try:
            tree = ast.parse(file.read_text())
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    name = (
                        dec.func.id
                        if isinstance(dec.func, ast.Name)
                        else getattr(dec.func, "attr", "")
                    )
                    if name != "workflow":
                        continue
                    row = dict(
                        hub=Path(hub_dir).name.lower(),
                        name=node.name,
                        source=str(file.relative_to(hub_dir)),
                        schedule=None,
                        timezone="UTC",
                        error=None,
                    )
                    try:
                        args = {
                            k.arg: ast.literal_eval(k.value)
                            for k in dec.keywords
                            if k.arg in ("schedule", "timezone")
                        }
                        row["schedule"] = (
                            ast.literal_eval(dec.args[0])
                            if dec.args
                            else args.get("schedule")
                        )
                        row["timezone"] = args.get("timezone") or "UTC"
                        if node.name in seen:
                            raise ValueError("duplicate workflow name within hub")
                        seen.add(node.name)
                        if row["schedule"]:
                            next_after(
                                row["schedule"],
                                row["timezone"],
                                datetime.now(timezone.utc),
                            )
                    except Exception as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"
                    rows.append(row)
        except Exception as exc:
            rows.append(
                dict(
                    hub=Path(hub_dir).name.lower(),
                    name=file.parent.name,
                    source=str(file.relative_to(hub_dir)),
                    schedule=None,
                    timezone="UTC",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return rows


def catalog(hub_dir) -> list[dict]:
    from .boot import schedules_enabled
    from ..access import store_for
    from ..deployment import read

    gs = store_for(hub_dir)
    health = gs.runtime_health(Path(hub_dir).name.lower())
    enabled = health.get("enabled", schedules_enabled() or bool(read(hub_dir)))
    now = datetime.now(timezone.utc)
    # A scheduled workflow is 'stale' when the deployment says schedules are
    # enabled but no dispatcher heartbeat has landed recently.
    stale = enabled and _stale(health.get("heartbeat"), now)
    rows = definitions(hub_dir)
    for row in rows:
        row["enabled"] = enabled and not row["error"]
        if row["error"] or health.get("error"):
            row["state"] = "error"
        elif not enabled:
            row["state"] = "disabled"
        elif not row["schedule"]:
            row["state"] = "manual"
        elif stale:
            row["state"] = "stale"
        else:
            row["state"] = "scheduled"
        row["error"] = row["error"] or health.get("error")
        row["next_run"] = (
            next_after(row["schedule"], row["timezone"], now).isoformat()
            if row["enabled"] and row["schedule"]
            else None
        )
        row["last_dispatch"] = health.get("last_dispatch")
        row["missed"] = health.get("missed", 0)
        row["heartbeat"] = health.get("heartbeat")
        row["downtime"] = health.get("downtime")
    return rows


def runs(hub_dir, *, name=None, run_id=None, limit=50, offset=0) -> list[dict]:
    from dbos import DBOSClient
    from .runtime import _app_name

    url = db.dbos_url(hub_dir)
    # No DBOS schema is created by observation.
    if url.startswith("sqlite:///") and not Path(url[len("sqlite:///") :]).exists():
        return []
    app = _app_name(Path(hub_dir).name)
    client = DBOSClient(
        system_database_url=url, application_name=app, retry_connection_errors=False
    )
    try:
        result = client.list_workflows(
            name=name,
            workflow_ids=[run_id] if run_id else None,
            application_name=app,
            limit=limit,
            offset=offset,
            sort_desc=True,
            load_input=False,
            load_output=True,
        )
        rows = []
        for w in result:
            started = w.dequeued_at or w.created_at
            completed = w.completed_at
            row = dict(
                hub=Path(hub_dir).name.lower(),
                id=w.workflow_id,
                name=w.name,
                status=w.status,
                started=started,
                completed=completed,
                duration_ms=completed - started if completed and started else None,
                output=str(w.output)[:8000] if w.output is not None else None,
                error=str(w.error)[:8000] if w.error else None,
            )
            if run_id:
                row["steps"] = [
                    dict(
                        name=x["function_name"],
                        started=x.get("started_at_epoch_ms"),
                        completed=x.get("completed_at_epoch_ms"),
                        error=str(x["error"])[:4000] if x.get("error") else None,
                        output=(
                            str(x["output"])[:4000]
                            if x.get("output") is not None
                            else None
                        ),
                    )
                    for x in client.list_workflow_steps(w.workflow_id)
                ]
            rows.append(row)
        return rows
    finally:
        client.destroy()
