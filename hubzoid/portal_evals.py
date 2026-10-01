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

from fastapi import HTTPException, APIRouter, Depends, Request
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
    return sorted((p for p in d.glob("*.json") if p.name != "index.json" and not p.is_symlink() and STAMP_RE.match(p.stem)),
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


def register(router: APIRouter, hub_dir: Path, *, require_admin: Callable,
             require_hub: Callable, check_mutation=None) -> None:
    """Read-only results. All access checks precede file reads or redirects."""
    from fastapi import Query
    from fastapi.responses import RedirectResponse
    from .evals import report, calls
    from . import deployment
    from .inbound.run import hub_slug

    def location(admin, hub, request):
        path = Path(require_hub(admin, hub))
        if path.resolve() != Path(hub_dir).resolve():
            slug = hub_slug(path, {})
            # A gateway routes this exact prefix to the owning bridge. Do not
            # open another bridge's private files from the Console bridge.
            return path, RedirectResponse('/b/' + slug + request.url.path + '?' + request.url.query)
        return path, None

    @router.get('/evals')
    def evals(request: Request, hub: str, admin=Depends(require_admin)):
        path, redirect = location(admin, hub, request)
        if redirect is not None:
            return redirect
        folder, cases, errors = read_cases(path)
        rows, total = report.summaries(path, limit=200)
        latest = {}
        for row in rows:
            for case in row['cases']:
                latest.setdefault(case['name'], dict(stamp=row['stamp'], passed=case['passed'],
                    reason='', finished=row['finished'], trigger=row['trigger']))
        # Definitions for account-scoped cases can contain private assertions.
        output = []
        for case in cases:
            row = _case_row(case)
            if case.run_as and normalize(case.run_as) != normalize(admin.subject):
                row.update(prompt='', checks=[], run_as=None)
            output.append(dict(row, latest=latest.get(case.name)))
        return dict(hub=hub, folder=folder, docs=DOCS, cases=output, errors=errors,
                    runs=total, active=None, last=None, state_error=None)

    @router.get('/evals/runs')
    def eval_runs(request: Request, hub: str, offset: int = Query(0, ge=0),
                  limit: int = Query(50, ge=1, le=200), admin=Depends(require_admin)):
        path, redirect = location(admin, hub, request)
        if redirect is not None:
            return redirect
        rows, total = report.summaries(path, offset=offset, limit=limit)
        return dict(hub=hub, runs=[{k:v for k,v in r.items() if k != 'cases'} for r in rows], total=total)

    @router.get('/evals/runs/{stamp}')
    def eval_run(request: Request, stamp: str, hub: str, admin=Depends(require_admin)):
        path, redirect = location(admin, hub, request)
        if redirect is not None:
            return redirect
        if not STAMP_RE.fullmatch(stamp) or stamp == 'index':
            raise HTTPException(422, 'Invalid eval run id')
        file = _runs_dir(path) / (stamp + '.json')
        loaded = load(file) if file.is_file() and not file.is_symlink() else None
        if loaded is None:
            raise HTTPException(404, 'Eval run not found')
        raw, suite = loaded
        _, current, _ = read_cases(path)
        prompts = {c.name: c.prompt for c in current}
        details = []
        for rc, result in zip(_raw_cases(raw), suite.cases):
            owner = rc.get('run_as') or raw.get('run_as')
            visible = not owner or normalize(owner) == normalize(admin.subject)
            if visible:
                detail = _case_detail(rc, result, prompts.get(result.name))
            else:
                detail = dict(name=result.name, passed=result.passed, duration=result.duration,
                    tags=[], reason='', error=None, checks=[dict(kind='check', passed=c.passed, detail='') for c in result.checks],
                    judge=None, answer='', prompt=None, turns=None, tools=[], run_as=None,
                    private=True)
            details.append(calls.redact(detail))
        summary = _summary(stamp, raw, suite)
        summary['run_as'] = None
        return dict(summary, hub=hub, cases=details)
