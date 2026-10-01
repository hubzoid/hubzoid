# Hubzoid admin portal. Apache-2.0 licensed like the rest of the repository.
"""Evals in the Console: a hub's cases, their results, and starting a run.

Routes, under `/portal/api` (registered by `portal.build_router`):

  GET  /evals?hub=<key>               cases, each with its latest result, plus
                                      the eval run in progress and the last one
  GET  /evals/runs?hub=<key>          result files, newest first
  GET  /evals/runs/{stamp}?hub=<key>  one result file: every case in full
  POST /evals/run                     {hub, confirm, cases?, judge?}: start a run

Who: the hub's administrators, the same gate as the hub's other Console
screens (an organization administrator, or Manage access for that hub). A
write must come from the Console's own origin unless an API key sent it.

Results are the files `hubzoid.evals.report.save` writes,
`<hub>/.hubzoid/evals/<stamp>.json`. Schema 1 and schema 2 files are both
read: `SuiteResult.from_dict` gives the shared fields, the raw JSON the schema
2 ones (trigger, run_as, tool calls with arguments, turns). A run id (the
stamp) is the file name without `.json`, checked against a strict pattern
before any path is built, so no request can name a file outside the folder.
Secret-looking argument values are redacted again on the way out.

Starting a run makes paid model calls (every case runs the agent, and a judged
case adds a grading call), so the request must carry `confirm: true`. The run
is queued as the `hz_eval_console` DBOS workflow on the hub's own engine
(`workflows/markdown.py`), through a DBOS client on the hub's DBOS database:
the database the Console already reads run history from. In a gateway the
hub's own bridge picks it up, whichever bridge serves the Console. The run is
durable, listed with the hub's other runs, and refused with 409 while another
eval run of that hub (scheduled or from the Console) is queued or running.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .access.identity import normalize
from .access.service import Denied

log = logging.getLogger("hubzoid.portal")

# A results file name without `.json`: the stamp `report.save` writes
# (YYYYmmdd_HHMMSS) or an explicit one. Letters, digits, `_` and `-` only.
STAMP_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,79}$")

DOCS = "https://github.com/hubzoid/hubzoid/blob/main/docs/evals.md"

_SECRET_KEY = re.compile(r"token|password|passwd|secret|api[_-]?key|authorization|cookie",
                         re.IGNORECASE)
_ACTIVE = ["PENDING", "ENQUEUED"]
# Two Console requests in this process never both pass the "nothing running"
# check; the queue's deduplication id covers other processes.
_START_LOCK = threading.Lock()
_DEDUP_ID = "evals-console"


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=200)
    # Running evals makes paid model calls: the caller says so explicitly.
    confirm: bool = False
    # None: every enabled case. Otherwise these case names.
    cases: list[str] | None = Field(default=None, max_length=500)
    judge: bool = True


# ---- reading cases ----------------------------------------------------------

def _case_checks(case) -> list[str]:
    """What a case checks, in a few words each (as `hubzoid eval list`)."""
    out = []
    if case.expect_tools:
        out.append("expects " + ", ".join(case.expect_tools))
    if case.forbid_tools:
        out.append("forbids " + ", ".join(case.forbid_tools))
    for tool, args in (getattr(case, "expect_tool_args", None) or {}).items():
        out.append(f"expects {tool} with {', '.join(str(k) for k in (args or {}))}")
    if case.contains:
        out.append(f"contains {len(case.contains)}")
    if case.not_contains:
        out.append(f"not_contains {len(case.not_contains)}")
    return out


def read_cases(hub_path: Path) -> tuple[bool, list, list[dict]]:
    """(the hub has an evals folder, parsed cases, unreadable files). One bad
    file never hides the others; it is listed with its error instead."""
    from ._fs import resolve_bucket
    from .evals import cases as cases_lib

    root = resolve_bucket(hub_path, "evals")
    if root is None:
        return False, [], []
    found, errors = [], []
    for path in sorted(root.glob("*.md"), key=lambda p: p.name.lower()):
        if path.name.startswith(("_", ".")):
            continue
        try:
            found.append(cases_lib.parse(path))
        except cases_lib.EvalCaseError as exc:
            errors.append(dict(file=f"{root.name}/{path.name}", error=str(exc)))
    return True, found, errors


def _case_row(case) -> dict:
    turns = getattr(case, "turns", None) or []
    return dict(
        name=case.name,
        tags=list(case.tags),
        schedule=case.schedule,
        judged=case.is_judged,
        turns=len(turns) or 1,
        run_as=getattr(case, "run_as", None),
        enabled=case.enabled,
        checks=_case_checks(case),
        prompt=(turns[0] if turns else case.prompt)[:500],
    )


# ---- reading results --------------------------------------------------------

def _runs_dir(hub_path: Path) -> Path:
    from .evals import report

    return report.runs_dir(hub_path)


def run_files(hub_path: Path) -> list[Path]:
    """Result files, newest first (the stamp sorts by time)."""
    d = _runs_dir(hub_path)
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.json") if STAMP_RE.match(p.stem)),
                  key=lambda p: p.name, reverse=True)


@functools.lru_cache(maxsize=256)
def _parse(path: str, mtime_ns: int, size: int):
    """(raw dict, SuiteResult) for one file version, or None when unreadable.
    Keyed by modification time and size, so a rewritten file is read again.
    Callers never mutate the cached raw dict."""
    from .evals.results import SuiteResult

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw, SuiteResult.from_dict(raw)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        log.warning("evals: skipping unreadable results file %s", Path(path).name)
        return None


def load(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return _parse(str(path), st.st_mtime_ns, st.st_size)


def _schema(raw: dict) -> int:
    try:
        return int(raw.get("schema") or 1)
    except (TypeError, ValueError):
        return 1


def _raw_cases(raw: dict) -> list[dict]:
    cases = raw.get("cases")
    return [c if isinstance(c, dict) else {} for c in cases] if isinstance(cases, list) else []


def _passed(raw_case: dict, result) -> bool:
    """The verdict the file recorded; recomputed only if it is missing."""
    value = raw_case.get("passed")
    return value if isinstance(value, bool) else result.passed


def _reason(raw_case: dict, result) -> str:
    value = raw_case.get("reason")
    return value if isinstance(value, str) else result.reason


def _trigger(raw: dict) -> str | None:
    """Schema 1 files predate triggers (null); schema 2 defaults to "cli"."""
    if _schema(raw) < 2:
        return None
    value = raw.get("trigger")
    return value if isinstance(value, str) and value else "cli"


def _summary(stamp: str, raw: dict, suite) -> dict:
    verdicts = [_passed(rc, r) for rc, r in zip(_raw_cases(raw), suite.cases)]
    run_as = raw.get("run_as")
    return dict(
        stamp=stamp,
        schema=_schema(raw),
        trigger=_trigger(raw),
        started=suite.started_at or None,
        finished=suite.finished_at or None,
        model=suite.model or None,
        judge_model=suite.judge_model,
        judged=suite.judged,
        run_as=run_as if isinstance(run_as, str) else None,
        passed=sum(verdicts),
        failed=len(verdicts) - sum(verdicts),
        total=len(verdicts),
    )


def redact(value: Any, depth: int = 0) -> Any:
    """Arguments with secret-looking keys' values replaced (evals-core already
    does this when saving; repeated here for older or hand-made files)."""
    if depth > 8:
        return "[…]"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value]
    return value


def _int_or_none(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tools(raw_case: dict, result) -> list[dict]:
    """Every tool call in order. Schema 2 records name, arguments, outcome and
    duration; a schema 1 file only the names."""
    calls = raw_case.get("tools")
    if isinstance(calls, list):
        out = []
        for c in calls:
            if not isinstance(c, dict):
                continue
            ok = c.get("ok")
            preview, error = c.get("preview"), c.get("error")
            out.append(dict(
                name=str(c.get("name") or "?"),
                args=redact(c.get("args")) if c.get("args") is not None else None,
                ok=ok if isinstance(ok, bool) else None,
                error=str(error) if error else None,
                duration_ms=_int_or_none(c.get("duration_ms")),
                preview=str(preview)[:500] if preview else None,
                turn=_int_or_none(c.get("turn")),
            ))
        return out
    return [dict(name=n, args=None, ok=None, error=None, duration_ms=None, preview=None, turn=1)
            for n in result.tool_calls]


def _turns(raw_case: dict) -> list[dict] | None:
    turns = raw_case.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    return [dict(prompt=str(t.get("prompt") or ""), response=str(t.get("response") or ""))
            for t in turns if isinstance(t, dict)]


def _case_detail(raw_case: dict, result, prompt: str | None) -> dict:
    judge = None
    if result.judge is not None:
        j = result.judge
        judge = dict(score=j.score, threshold=j.threshold, reasoning=j.reasoning,
                     model=j.model or None, error=j.error, passed=j.passed)
    run_as = raw_case.get("run_as")
    return dict(
        name=result.name,
        tags=list(result.tags),
        passed=_passed(raw_case, result),
        reason=_reason(raw_case, result),
        duration=round(result.duration, 3),
        error=result.error,
        checks=[c.to_dict() for c in result.checks],
        judge=judge,
        answer=result.response,
        prompt=prompt,
        turns=_turns(raw_case),
        tools=_tools(raw_case, result),
        run_as=run_as if isinstance(run_as, str) else None,
    )


def latest_by_case(hub_path: Path, names: set[str]) -> dict[str, dict]:
    """Each case's most recent recorded result: the newest file that ran it."""
    out: dict[str, dict] = {}
    for path in run_files(hub_path):
        if names <= set(out):
            break
        loaded = load(path)
        if loaded is None:
            continue
        raw, suite = loaded
        for rc, r in zip(_raw_cases(raw), suite.cases):
            if r.name in names and r.name not in out:
                out[r.name] = dict(stamp=path.stem, passed=_passed(rc, r),
                                   reason=_reason(rc, r),
                                   finished=suite.finished_at or suite.started_at or None,
                                   trigger=_trigger(raw))
    return out


# ---- the hub's eval runs on DBOS -------------------------------------------

def _client(hub_path: Path):
    """A DBOS client on the hub's DBOS database, or None before it exists."""
    from dbos import DBOSClient

    from . import db
    from .workflows.observe import _missing_sqlite
    from .workflows.runtime import _app_name

    url = db.dbos_url(hub_path)
    if _missing_sqlite(url):
        return None
    return DBOSClient(system_database_url=url, application_name=_app_name(hub_path.name),
                      retry_connection_errors=False)


def _run_info(w) -> dict:
    from .workflows import markdown

    args = list((w.input or {}).get("args") or []) if isinstance(w.input, dict) else []
    console = w.name == markdown.EVAL_CONSOLE_WORKFLOW
    names = args[0] if args and isinstance(args[0], list) else None
    output = w.output if isinstance(w.output, dict) else None
    return dict(
        id=w.workflow_id,
        source="console" if console else "schedule",
        status=w.status,
        created=w.created_at,
        started=w.dequeued_at or w.created_at,
        completed=w.completed_at,
        cases=names,
        judge=(args[1] if console and len(args) > 1 and isinstance(args[1], bool) else None),
        requested_by=(args[2] if console and len(args) > 2 and isinstance(args[2], str) else None),
        stamp=(output or {}).get("stamp"),
        # The hub's eval summary or a configuration error: shown to the hub's
        # administrators, who see the full results here anyway.
        error=str(w.error)[:2000] if w.error else None,
    )


def run_state(hub_path: Path) -> dict:
    """The eval run queued or in progress for this hub, and the last finished
    one (scheduled or from the Console). Either may be None."""
    from .workflows import markdown
    from .workflows.runtime import _app_name

    client = _client(hub_path)
    if client is None:
        return dict(active=None, last=None)
    app = _app_name(hub_path.name)
    try:
        rows = client.list_workflows(name=list(markdown.EVAL_WORKFLOWS), application_name=app,
                                     limit=10, sort_desc=True, load_input=True, load_output=True)
        active = client.list_workflows(name=list(markdown.EVAL_WORKFLOWS), status=_ACTIVE,
                                       application_name=app, limit=1, sort_desc=True,
                                       load_input=True, load_output=False)
    finally:
        client.destroy()
    done = next((w for w in rows if w.status not in _ACTIVE), None)
    return dict(active=_run_info(active[0]) if active else None,
                last=_run_info(done) if done else None)


def engine_problem(hub_dir: Path, hub_path: Path) -> str | None:
    """Why a run queued now would not start, or None. The hub's bridge starts
    its workflow engine when the hub has eval cases; the Console only queues."""
    from . import db
    from .access import store_for
    from .workflows.observe import _missing_sqlite

    health = store_for(hub_dir).runtime_health(hub_path.name)
    if health.get("enabled") is False or _missing_sqlite(db.dbos_url(hub_path)):
        detail = f" It reported: {health['error']}" if health.get("error") else ""
        return ("This agent's workflow engine isn't running, so evals can't start from the "
                "Console. It starts with the agent when the agent has eval cases, unless "
                "HUBZOID_DISABLE_SCHEDULE is set. Restart the agent, or run "
                "`hubzoid eval run` on the server." + detail)
    return None


class Busy(Exception):
    def __init__(self, run_id: str | None):
        super().__init__(run_id or "")
        self.run_id = run_id


def start_run(hub_path: Path, *, names: list[str] | None, judge: bool,
              requested_by: str) -> str:
    """Queue one Console eval run on the hub's markdown queue and return its
    id. Raises Busy while another eval run of the hub is queued or running."""
    from .workflows import markdown
    from .workflows.runtime import _app_name

    try:
        from dbos._error import DBOSQueueDeduplicatedError
    except ImportError:  # pragma: no cover - a DBOS without the class
        DBOSQueueDeduplicatedError = ()  # type: ignore[assignment]

    app = _app_name(hub_path.name)
    with _START_LOCK:
        client = _client(hub_path)
        if client is None:
            raise RuntimeError("the hub's workflow database does not exist yet")
        try:
            active = client.list_workflows(name=list(markdown.EVAL_WORKFLOWS), status=_ACTIVE,
                                           application_name=app, limit=1,
                                           load_input=False, load_output=False)
            if active:
                raise Busy(active[0].workflow_id)
            run_id = markdown.console_eval_run_id(hub_path.name)
            # No code version: the hub's bridge (running the latest code) takes
            # it, and a bridge that starts later re-queues it under its own.
            client.enqueue({
                "workflow_name": markdown.EVAL_CONSOLE_WORKFLOW,
                "queue_name": markdown.queue_name(hub_path.name),
                "workflow_id": run_id,
                "deduplication_id": _DEDUP_ID,
                "application_name": app,
            }, list(names) if names is not None else None, bool(judge), requested_by)
            return run_id
        except DBOSQueueDeduplicatedError as exc:  # another process just started one
            raise Busy(getattr(exc, "workflow_id", None)) from exc
        finally:
            client.destroy()


# ---- routes ----------------------------------------------------------------

def _refusal(fn):
    """A Denied refusal as `{"detail", "code"}`, as the rest of the Console."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Denied as exc:
            return JSONResponse({**exc.extra, "detail": exc.message, "code": exc.code},
                                status_code=exc.status)

    return wrapper


def register(router: APIRouter, hub_dir: Path, *, require_admin: Callable,
             require_hub: Callable, check_mutation: Callable) -> None:
    """Add the eval routes to the Console's router."""
    hub_dir = Path(hub_dir)

    def _state(hub_path: Path) -> dict:
        try:
            return run_state(hub_path)
        except Exception:  # noqa: BLE001 - the results still show
            log.exception("evals: run state unavailable for %s", hub_path.name)
            return dict(active=None, last=None, state_error=(
                "The eval run state is unavailable; check the workflow database and server logs."))

    @router.get("/evals")
    @_refusal
    def evals(hub: str, admin=Depends(require_admin)):
        hub_path = Path(require_hub(admin, hub))
        folder, cases, errors = read_cases(hub_path)
        latest = latest_by_case(hub_path, {c.name for c in cases})
        rows = [dict(_case_row(c), latest=latest.get(c.name)) for c in cases]
        return dict(hub=hub, folder=folder, docs=DOCS, cases=rows, errors=errors,
                    runs=len(run_files(hub_path)), **_state(hub_path))

    @router.get("/evals/runs")
    @_refusal
    def eval_runs(hub: str, admin=Depends(require_admin)):
        hub_path = Path(require_hub(admin, hub))
        runs = []
        for path in run_files(hub_path):
            loaded = load(path)
            if loaded is not None:
                runs.append(_summary(path.stem, *loaded))
        return dict(hub=hub, runs=runs)

    @router.get("/evals/runs/{stamp}")
    @_refusal
    def eval_run(stamp: str, hub: str, admin=Depends(require_admin)):
        hub_path = Path(require_hub(admin, hub))
        if not STAMP_RE.match(stamp):
            raise Denied(422, "invalid_run", "That is not an eval run id.")
        path = _runs_dir(hub_path) / f"{stamp}.json"
        loaded = load(path) if path.is_file() else None
        if loaded is None:
            raise Denied(404, "not_found", f"No eval run {stamp} is recorded for this agent.")
        raw, suite = loaded
        _, current, _ = read_cases(hub_path)
        prompts = {c.name: c.prompt for c in current}
        cases = [_case_detail(rc, r, prompts.get(r.name))
                 for rc, r in zip(_raw_cases(raw), suite.cases)]
        return dict(_summary(stamp, raw, suite), hub=hub, cases=cases)

    @router.post("/evals/run", status_code=202)
    @_refusal
    def start(request: Request, payload: EvalRunRequest, admin=Depends(require_admin)):
        check_mutation(request, admin)
        hub_path = Path(require_hub(admin, payload.hub))
        if payload.confirm is not True:
            raise Denied(422, "confirm_required",
                         "Running evals makes model calls. Send confirm: true to start.")
        folder, cases, errors = read_cases(hub_path)
        if errors:
            raise Denied(422, "bad_cases",
                         "Fix these case files first: "
                         + "; ".join(f"{e['file']}: {e['error']}" for e in errors),
                         extra={"errors": errors})
        enabled = [c.name for c in cases if c.enabled]
        names: list[str] | None = None
        if payload.cases is not None:
            names = list(dict.fromkeys(payload.cases))
            known = {c.name: c for c in cases}
            unknown = [n for n in names if n not in known]
            if unknown:
                raise Denied(422, "unknown_case", "No such case: " + ", ".join(unknown[:20]) + ".")
            off = [n for n in names if not known[n].enabled]
            if off:
                raise Denied(422, "disabled_case",
                             "These cases are disabled in their files: " + ", ".join(off[:20]) + ".")
            if not names:
                raise Denied(422, "no_cases", "Choose at least one case.")
        elif not enabled:
            raise Denied(422, "no_cases", "This agent has no enabled eval cases to run.")
        problem = engine_problem(hub_dir, hub_path)
        if problem:
            raise Denied(409, "engine_off", problem)
        try:
            run_id = start_run(hub_path, names=names, judge=payload.judge,
                               requested_by=normalize(admin.subject))
        except Busy as exc:
            raise Denied(409, "eval_running",
                         "An eval run for this agent is already queued or running. "
                         "Wait for it to finish.", extra={"run_id": exc.run_id})
        from .access import store_for

        try:
            store_for(hub_dir).audit_run_control(payload.hub, "evals_run", run_id,
                                                 actor=normalize(admin.subject))
        except Exception:  # noqa: BLE001 - the run is queued; say so in the log
            log.exception("evals: could not record the eval run %s in the audit", run_id)
        return dict(ok=True, run_id=run_id, status="ENQUEUED",
                    cases=names if names is not None else enabled, judge=payload.judge)
