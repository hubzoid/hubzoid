"""Definition inspection without execution; execution history through DBOS API.

Run history is operational metadata for the hub's managers: workflow, status,
timing and a sanitized failure summary. What a run produced (its return value,
step outputs, and error text that may carry data) belongs to the account the run
acted as, and is shown only to that account (`viewer`). The account comes from
the run's recorded identity step; a run whose account cannot be established is
treated as private. A legacy service run acts for no person, so its detail stays
visible to the hub's managers as before. Hub management or organization
administration alone never reveals a person's run results.
"""

from __future__ import annotations

import ast
import logging
from datetime import datetime, timezone
from pathlib import Path

from .schedule_grammar import next_after
from .. import db

log = logging.getLogger("hubzoid.workflows")

# Steps whose output records who the run acted as (operational metadata).
_IDENTITY_STEPS = frozenset({"hz_run_identity", "hz_md_identity"})
# Hubzoid's own configuration errors: they name settings, never run data.
_SAFE_ERRORS = frozenset({"IdentityError", "ScheduleError"})


def _error_summary(err) -> str | None:
    """A failure summary safe for managers: the error type, plus the message
    only for Hubzoid's own configuration errors."""
    if not err:
        return None
    if isinstance(err, BaseException):
        kind = type(err).__name__
        if kind in _SAFE_ERRORS:
            return str(err)[:2000]
        return f"{kind} (details are visible to the account the run acted as)"
    return "The run failed (details are visible to the account the run acted as)"


def _identity_of(steps) -> dict | None:
    for x in steps or ():
        if x.get("function_name") in _IDENTITY_STEPS and isinstance(x.get("output"), dict):
            return x["output"]
    return None


def _run_owner(client, workflow_id: str) -> dict | None:
    """Who the run acted as, from its recorded identity step, or None."""
    try:
        return _identity_of(client.list_workflow_steps(workflow_id))
    except Exception:  # noqa: BLE001 — unknown owner: treated as private
        log.exception("workflows: could not read the identity of run %s", workflow_id)
        return None


def may_see_results(owner: dict | None, viewer: str | None) -> bool:
    """Whether `viewer` may see what a run produced. Fails closed."""
    if not owner:
        return False
    if owner.get("source") == "legacy-service":
        return True          # a hub service run: no person's data or connections
    subject = (owner.get("subject") or "").strip().lower()
    return bool(viewer) and subject == viewer.strip().lower()

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
                        run_as=None,
                    )
                    try:
                        args = {
                            k.arg: ast.literal_eval(k.value)
                            for k in dec.keywords
                            if k.arg in ("schedule", "timezone", "run_as")
                        }
                        row["run_as"] = args.get("run_as")
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
    paused = gs.paused_workflows(Path(hub_dir).name)
    for row in rows:
        row["enabled"] = enabled and not row["error"]
        if row["error"] or health.get("error"):
            row["state"] = "error"
        elif row["name"] in paused:
            row["state"] = "paused"
        elif not row["schedule"]:
            row["state"] = "manual"
        elif not enabled:
            row["state"] = "disabled"
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
    rows = rows + markdown_catalog(hub_dir)
    from . import identity

    for row in rows:
        # Who it runs as, by the same resolution a run uses (CLI and Console).
        # The legacy service subject is workflow:<name> / workflow:md:<task>.
        row["runs_as"] = identity.summary(hub_dir, run_as=row.get("run_as"),
                                          legacy_subject=f"workflow:{row['name']}")
    return rows


def markdown_catalog(hub_dir) -> list[dict]:
    """The hub's `schedule/*.md` tasks, in the same row shape as code workflows.
    They run whenever their files exist (HUBZOID_DISABLE_SCHEDULE=1 stops them)."""
    import os

    from .. import scheduling as sch

    hub_dir = Path(hub_dir)
    tasks, problems = sch.load_tasks(hub_dir)
    disabled = (os.environ.get("HUBZOID_DISABLE_SCHEDULE") or "").strip().lower() in (
        "1", "true", "yes", "on")
    state = sch.ScheduleState(hub_dir)
    now = datetime.now()
    from ..access import store_for

    paused = store_for(hub_dir).paused_workflows(hub_dir.name)
    rows = []
    for t in tasks:
        on = t.enabled and not disabled and f"md:{t.name}" not in paused
        nxt = None if (t.is_webhook or not on) else sch.next_fire_for(t, state, now)
        last = state.get(t.name)
        rows.append(dict(
            hub=hub_dir.name.lower(), name=f"md:{t.name}", kind="markdown",
            source=f"schedule/{t.name}.md",
            schedule=(f"on webhook {t.on_webhook}" if t.is_webhook else t.schedule),
            timezone="server local time", error=None, enabled=on,
            state=("paused" if f"md:{t.name}" in paused else "definition-disabled" if not t.enabled
                   else "disabled" if disabled else "event" if t.is_webhook else "scheduled"),
            next_run=nxt.astimezone().isoformat() if nxt else None,
            last_dispatch=last.get("last_fired_iso"), missed=0,
            heartbeat=None, downtime=None, run_as=t.run_as,
        ))
    for problem in problems:
        rows.append(dict(hub=hub_dir.name.lower(), name="md:?", kind="markdown",
                         source="schedule/", schedule=None, timezone="server local time",
                         error=problem, enabled=False, state="error", next_run=None,
                         last_dispatch=None, missed=0, heartbeat=None, downtime=None))
    return rows


# UI-facing status buckets → the concrete DBOS workflow states each covers.
# Filtering happens server-side (DBOS applies `status` before pagination), so the
# bucket a caller asks for is expanded here and passed straight to list_workflows.
STATUS_BUCKETS = {
    "succeeded": ["SUCCESS"],
    "failed": ["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"],
    "running": ["PENDING", "ENQUEUED"],
    "cancelled": ["CANCELLED"],
}
_KNOWN_STATUSES = {
    "PENDING",
    "SUCCESS",
    "ERROR",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "CANCELLED",
    "ENQUEUED",
    "DELAYED",
}


def resolve_statuses(values) -> list[str] | None:
    """Expand UI status buckets (and pass through raw DBOS states) to a flat,
    de-duplicated list for ``list_workflows(status=...)``.

    An *absent* filter (None/""/whitespace) returns ``None`` (no filtering). A
    *supplied* filter with any unrecognized token raises ``ValueError`` — it is never
    silently dropped, because dropping it to ``None`` would widen the query to every
    status. Callers turn that ValueError into a 422 rather than returning everything."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    tokens: list[str] = []
    for v in values:
        for token in str(v).split(","):
            token = token.strip()
            if token:
                tokens.append(token)
    if not tokens:
        return None
    out: list[str] = []
    for token in tokens:
        if token in STATUS_BUCKETS:
            out.extend(STATUS_BUCKETS[token])
        elif token.upper() in _KNOWN_STATUSES:
            out.append(token.upper())
        else:
            raise ValueError(f"unknown run status filter: {token!r}")
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def _run_row(hub_name: str, w, *, visible: bool = False, owner: dict | None = None) -> dict:
    # `created` is the ordering/pagination key: it is what DBOS's own `sort_desc`
    # orders by (created_at), so merging and paginating cross-agent results by the
    # same field keeps the over-fetch window valid. `started` (dequeued-or-created)
    # is a separate DISPLAY value for when execution actually began — it must not be
    # used for ordering, since DBOS cannot page by it.
    created = w.created_at
    started = w.dequeued_at or w.created_at
    completed = w.completed_at
    from . import markdown

    name = w.name
    if w.name == markdown.MD_WORKFLOW:
        name = f"md:{markdown.task_name_from_id(w.workflow_id) or '?'}"
    elif w.name == markdown.EVAL_WORKFLOW:
        name = "evals"
    return dict(
        hub=hub_name,
        id=w.workflow_id,
        name=name,
        status=w.status,
        created=created,
        started=started,
        completed=completed,
        duration_ms=completed - started if completed and started else None,
        output=(str(w.output)[:8000] if w.output is not None else None) if visible else None,
        error=(str(w.error)[:8000] if w.error else None) if visible else _error_summary(w.error),
        redacted=(not visible) and w.output is not None,
        run_as=(owner or {}).get("subject"),
    )


def _order_key(row: dict):
    # Newest first by creation time; workflow id as a stable tie-break so equal
    # timestamps paginate deterministically (no gaps, no duplicates).
    return (row["created"] or 0, row["id"])


def _iso(ms: int) -> str:
    # DBOS start_time/end_time are ISO strings it converts back to epoch ms via
    # int(datetime.fromisoformat(s).timestamp() * 1000); round-trips exactly at ms.
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _missing_sqlite(url: str) -> bool:
    # Observation never creates a DBOS schema, so an absent SQLite file means the
    # bridge has not run yet — treat it as "no runs" rather than an error.
    return url.startswith("sqlite:///") and not Path(url[len("sqlite:///") :]).exists()


def runs(
    hub_dir,
    *,
    name=None,
    run_id=None,
    statuses=None,
    start=None,
    end=None,
    limit=50,
    offset=0,
    viewer: str | None = None,
    trusted: bool = False,
) -> list[dict]:
    """Single-agent run history. Includes per-step detail when ``run_id`` is set.
    Filters (name/status/date/run-id) are applied by DBOS before pagination.

    Results are redacted unless ``viewer`` is the account the run acted as (see
    the module docstring). ``trusted`` is only for the server's own operator
    (the CLI on the box), who can read the database directly anyway."""
    from dbos import DBOSClient
    from .runtime import _app_name

    url = db.dbos_url(hub_dir)
    if _missing_sqlite(url):
        return []
    app = _app_name(Path(hub_dir).name)
    hub_name = Path(hub_dir).name.lower()
    client = DBOSClient(
        system_database_url=url, application_name=app, retry_connection_errors=False
    )
    prefix = None
    if name and name.startswith("md:"):
        # Markdown tasks share one DBOS workflow; the run id carries the task.
        from . import markdown

        name, prefix = markdown.MD_WORKFLOW, f"{name}:"
    try:
        result = client.list_workflows(
            name=name,
            workflow_id_prefix=prefix,
            workflow_ids=[run_id] if run_id else None,
            status=resolve_statuses(statuses),
            start_time=start,
            end_time=end,
            application_name=app,
            limit=limit,
            offset=offset,
            sort_desc=True,
            load_input=False,
            load_output=True,
        )
        rows = []
        for w in result:
            steps = client.list_workflow_steps(w.workflow_id) if run_id else None
            owner = _identity_of(steps) if run_id else _run_owner(client, w.workflow_id)
            visible = trusted or may_see_results(owner, viewer)
            row = _run_row(hub_name, w, visible=visible, owner=owner)
            if run_id:
                row["steps"] = [_step_row(x, visible) for x in steps]
            rows.append(row)
        return rows
    finally:
        client.destroy()


def _step_row(x: dict, visible: bool) -> dict:
    # The identity step says who the run acted as: metadata, always shown.
    shown = visible or x.get("function_name") in _IDENTITY_STEPS
    return dict(
        name=x["function_name"],
        started=x.get("started_at_epoch_ms"),
        completed=x.get("completed_at_epoch_ms"),
        error=(str(x["error"])[:4000] if x.get("error") else None) if shown
        else _error_summary(x.get("error")),
        output=(str(x["output"])[:4000] if x.get("output") is not None else None)
        if shown else None,
        redacted=(not shown) and x.get("output") is not None,
    )


def _source_groups(hubs):
    """(dbos_url) -> [(app_name, display_key)]. display_key is the deployment's hub
    key (what the portal routes on), which need not equal the folder name. Sources
    whose SQLite DB doesn't exist yet are skipped."""
    from collections import defaultdict
    from .runtime import _app_name

    groups: "dict[str, list[tuple[str, str]]]" = defaultdict(list)
    for h in hubs:
        hd = Path(h["path"])
        url = db.dbos_url(hd)
        if _missing_sqlite(url):
            continue
        groups[url].append((_app_name(hd.name), h.get("key") or hd.name.lower()))
    return groups


def _query_source(url, members, *, name, run_id, statuses, start, end, limit,
                  viewer=None) -> list[dict]:
    """Run one ``list_workflows`` against a single DBOS system DB (which may host
    several agents, on shared Postgres), mapping each row back to its hub key."""
    from dbos import DBOSClient

    apps = [app for app, _ in members]
    app_to_hub = {app: key for app, key in members}
    single = members[0][1] if len(members) == 1 else None
    client = DBOSClient(
        system_database_url=url, application_name=apps[0], retry_connection_errors=False
    )
    try:
        result = client.list_workflows(
            name=name,
            workflow_ids=[run_id] if run_id else None,
            status=statuses,
            start_time=start,
            end_time=end,
            application_name=apps,
            limit=limit,
            offset=0,
            sort_desc=True,
            load_input=False,
            load_output=True,
        )
        rows = []
        for w in result:
            hub_name = app_to_hub.get(w.application_name) or single
            if hub_name is not None:
                owner = _run_owner(client, w.workflow_id)
                rows.append(_run_row(hub_name, w, visible=may_see_results(owner, viewer),
                                     owner=owner))
        return rows
    finally:
        client.destroy()


def runs_across(
    hubs,
    *,
    name=None,
    run_id=None,
    statuses=None,
    start=None,
    end=None,
    limit=50,
    offset=0,
    viewer: str | None = None,
) -> dict:
    """Cross-agent run history over the authorized ``hubs`` (each a mapping with
    ``path`` — the caller has already scoped this to manageable agents).

    Ordering/pagination is by ``created_at`` (the only key DBOS ``sort_desc`` uses),
    with workflow id as a tie-break. DBOS orders each source by ``created_at`` ALONE,
    so a truncated over-fetch can split a group of rows sharing a created_at
    millisecond — re-sorting that truncated window by id would skip and duplicate
    rows across pages. To avoid that, once the page boundary is known we refetch the
    COMPLETE group of rows at exactly the boundary timestamp before applying the id
    tie-break. This is bounded: all rows newer than the boundary already fit in the
    initial over-fetch (proof: no single source can hold more than the window's worth
    of strictly-newer rows without pushing the boundary up), and only the one boundary
    timestamp's group is refetched — never the full history.
    """
    resolved = resolve_statuses(statuses)
    groups = _source_groups(hubs)

    if run_id:
        merged = []
        for url, members in groups.items():
            merged += _query_source(
                url, members, name=name, run_id=run_id, statuses=resolved,
                start=start, end=end, limit=None, viewer=viewer,
            )
        merged.sort(key=_order_key, reverse=True)
        return {"runs": merged, "has_more": False}

    want = offset + limit + 1
    phase1: "dict[str, list[dict]]" = {}
    capped: "dict[str, bool]" = {}
    for url, members in groups.items():
        rows = _query_source(
            url, members, name=name, run_id=None, statuses=resolved,
            start=start, end=end, limit=want, viewer=viewer,
        )
        phase1[url] = rows
        capped[url] = len(rows) >= want

    merged = [r for rows in phase1.values() for r in rows]
    merged.sort(key=_order_key, reverse=True)

    # No source hit its limit → the over-fetch already holds every candidate row, so
    # the merged order is complete and the simple slice is correct.
    if not any(capped.values()):
        return {"runs": merged[offset : offset + limit], "has_more": len(merged) > offset + limit}

    idx = min(offset + limit, len(merged)) - 1
    if idx < 0:
        return {"runs": [], "has_more": False}
    boundary = merged[idx]["created"] or 0

    # Rows strictly newer than the boundary are already complete in the over-fetch.
    by_key: "dict[tuple, dict]" = {
        (r["hub"], r["id"]): r for r in merged if (r["created"] or 0) > boundary
    }
    # Complete the boundary-timestamp group: capped sources may have hidden tied rows
    # below the window, so refetch exactly created_at == boundary from them; a source
    # that did not cap already returned all of its boundary rows in the over-fetch.
    b_iso = _iso(boundary)
    for url, members in groups.items():
        if capped[url]:
            group = _query_source(
                url, members, name=name, run_id=None, statuses=resolved,
                start=b_iso, end=b_iso, limit=None, viewer=viewer,
            )
        else:
            group = [r for r in phase1[url] if (r["created"] or 0) == boundary]
        for r in group:
            by_key[(r["hub"], r["id"])] = r

    complete = sorted(by_key.values(), key=_order_key, reverse=True)
    page = complete[offset : offset + limit]

    if len(complete) > offset + limit:
        has_more = True
    else:
        # The page reaches the boundary; more exist only if any capped source has a
        # row strictly older than it. One bounded 1-row probe per capped source.
        older = _iso(boundary - 1)
        has_more = any(
            capped[url]
            and _query_source(
                url, members, name=name, run_id=None, statuses=resolved,
                start=start, end=older, limit=1,
            )
            for url, members in groups.items()
        )
    return {"runs": page, "has_more": has_more}


def run_counts(hubs, since: str) -> dict[str, dict]:
    """Runs started since `since` (ISO) per hub key: total, failed, cancelled.
    Hubs with no DBOS database yet are absent (they have never run anything)."""
    from dbos import DBOSClient

    out: dict[str, dict] = {}
    for url, members in _source_groups(hubs).items():
        apps = [app for app, _ in members]
        app_to_hub = dict(members)
        single = members[0][1] if len(members) == 1 else None
        client = DBOSClient(system_database_url=url, application_name=apps[0],
                            retry_connection_errors=False)
        try:
            rows = client.list_workflows(start_time=since, application_name=apps,
                                         load_input=False, load_output=False)
        finally:
            client.destroy()
        for _, key in members:
            out.setdefault(key, {"runs": 0, "failed": 0, "cancelled": 0})
        for w in rows:
            key = app_to_hub.get(w.application_name) or single
            if key is None:
                continue
            counts = out[key]
            counts["runs"] += 1
            if w.status in ("ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
                counts["failed"] += 1
            elif w.status == "CANCELLED":
                counts["cancelled"] += 1
    return out
