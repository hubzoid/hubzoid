"""Optional: push an eval run to Langfuse as a dataset run with scores.

Langfuse is an **upgrade, never a dependency**. Every eval surface works with
nothing installed — the terminal table, the run JSON, `--compare`, `eval
status`. This module adds run history beyond the last two files, score trends
over months, the judge's reasoning next to the trace, and a UI a non-CLI
person can open. A hub with no endpoint configured never touches this code
path.

Configuration is the same one Hubzoid already documents for tracing
(`docs/OBSERVABILITY.md`), so an operator who turned tracing on gets eval
history for free:

    HUBZOID_OTEL_ENDPOINT=https://langfuse.internal/api/public/otel
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...

The OTel endpoint doubles as the discovery signal for "is Langfuse
configured": its host is where the ingestion API lives. Keys come from the
standard Langfuse env vars, or are decoded from `OTEL_EXPORTER_OTLP_HEADERS`
if that is how the operator set tracing up — no third place to configure.

Everything here is best-effort. A Langfuse outage must never turn a green
suite red: the caller catches, warns, and keeps the local JSON as the record.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .results import SuiteResult

log = logging.getLogger("hubzoid.evals")

# One dataset per hub. Runs accumulate inside it, which is what makes the
# score-over-time view meaningful.
DATASET_PREFIX = "hubzoid-evals"

# Marks every eval trace so production dashboards can filter them out. Eval
# traffic is synthetic; leaving it mixed in would quietly skew the latency and
# cost numbers an operator uses to understand real users.
EVAL_TAG = "hubzoid.eval"

_TIMEOUT = 15


def dataset_name(hub_dir: Path) -> str:
    return f"{DATASET_PREFIX}-{hub_dir.name}"


def _keys_from_otel_headers() -> tuple[str, str] | None:
    """Recover the Langfuse key pair from `OTEL_EXPORTER_OTLP_HEADERS`.

    Operators following docs/OBSERVABILITY.md set Basic auth there rather than
    the LANGFUSE_* vars. Reading it back means one place to configure, not two.
    """
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    match = re.search(r"Authorization\s*=\s*Basic\s+([A-Za-z0-9+/=]+)", raw)
    if not match:
        return None
    try:
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    public, sep, secret = decoded.partition(":")
    return (public, secret) if sep else None


def _base_url(endpoint: str) -> str:
    """Langfuse host from the OTLP endpoint (…/api/public/otel -> scheme://host)."""
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def config(hub_dir: Path) -> tuple[str, str, str] | None:
    """(base_url, public_key, secret_key), or None when not configured."""
    from .. import settings as settingslib

    endpoint = (settingslib.load(hub_dir).otel_endpoint or "").strip()
    if not endpoint:
        return None
    base = _base_url(endpoint)
    if not base:
        return None

    public = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    if not (public and secret):
        pair = _keys_from_otel_headers()
        if pair:
            public, secret = pair
    if not (public and secret):
        log.info("langfuse endpoint set but no keys — skipping eval push")
        return None
    return base, public, secret


def _timestamp(suite: SuiteResult) -> str:
    """An ISO-8601 timestamp Langfuse will accept: always carries an offset.

    Langfuse validates against a strict pattern requiring `Z` or `+HH:MM`, and
    rejects the whole event without one. `now_iso()` produces that today, but
    a run reloaded from an older JSON file may hold a naive string, so coerce
    rather than trust the input.
    """
    raw = (suite.finished_at or suite.started_at or "").strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.now()
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.replace(microsecond=0).isoformat()


def _events(hub_dir: Path, suite: SuiteResult, run_name: str) -> list[dict]:
    """Build the ingestion batch: one dataset item + scores per case.

    Uses the generic `/api/public/ingestion` batch endpoint rather than the
    Langfuse SDK, so evals add no dependency and work against any self-hosted
    version that speaks the documented ingestion contract.

    **Every score body carries its own `id`.** The docs call it optional, but a
    score posted without one is accepted with HTTP 201 and then silently never
    materialises — no error, no row, nothing in the trace. The SDK never hits
    this because it generates a UUID client-side before sending. The ids here
    are deterministic (they mirror the envelope id), so re-pushing the same run
    updates the same scores instead of duplicating them.
    """
    dataset = dataset_name(hub_dir)
    stamp = _timestamp(suite)
    events: list[dict] = []

    for i, case in enumerate(suite.cases):
        trace_id = f"eval-{run_name}-{case.name}"
        events.append({
            "id": f"{trace_id}-trace",
            "type": "trace-create",
            "timestamp": stamp,
            "body": {
                "id": trace_id,
                "name": f"eval/{case.name}",
                "sessionId": run_name,
                "input": {"case": case.name, "dataset": dataset},
                "output": case.response,
                "tags": [EVAL_TAG, f"hub:{suite.hub}", *case.tags],
                "metadata": {
                    EVAL_TAG: True,
                    "hub": suite.hub,
                    "run": run_name,
                    "model": suite.model,
                    "judge_model": suite.judge_model,
                    "tool_calls": case.tool_calls,
                    "duration_s": case.duration,
                    "error": case.error,
                },
            },
        })

        # The headline score: did the case pass at all.
        events.append({
            "id": f"{trace_id}-score-passed",
            "type": "score-create",
            "timestamp": stamp,
            "body": {
                "id": f"{trace_id}-score-passed",
                "traceId": trace_id, "name": "passed",
                "value": 1 if case.passed else 0,
                "dataType": "NUMERIC", "comment": case.reason or None,
            },
        })

        # One score per free assertion, so a Langfuse chart can show *which*
        # kind of check is degrading, not just that something is.
        for n, check in enumerate(case.checks):
            events.append({
                "id": f"{trace_id}-score-{n}",
                "type": "score-create",
                "timestamp": stamp,
                "body": {
                    "id": f"{trace_id}-score-{n}",
                    "traceId": trace_id, "name": check.kind,
                    "value": 1 if check.passed else 0,
                    "dataType": "NUMERIC", "comment": check.detail or None,
                },
            })

        if case.judge is not None and case.judge.error is None:
            events.append({
                "id": f"{trace_id}-score-judge",
                "type": "score-create",
                "timestamp": stamp,
                "body": {
                    "id": f"{trace_id}-score-judge",
                    "traceId": trace_id, "name": "judge",
                    "value": case.judge.score, "dataType": "NUMERIC",
                    "comment": case.judge.reasoning or None,
                },
            })
    return events


def push(hub_dir: Path, suite: SuiteResult, *, run_name: str | None = None) -> str | None:
    """Send the run to Langfuse. Returns a short status, or None if not configured.

    Raises only on a genuinely unexpected failure; the caller treats any
    exception as "skipped" and keeps going. The local JSON is the record.
    """
    cfg = config(hub_dir)
    if cfg is None:
        return None
    base, public, secret = cfg

    run_name = run_name or (suite.finished_at or suite.started_at).replace(":", "").replace("-", "")
    events = _events(hub_dir, suite, run_name)
    if not events:
        return None

    import httpx

    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    resp = httpx.post(
        f"{base}/api/public/ingestion",
        json={"batch": events},
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    # The ingestion endpoint answers 207 Multi-Status and reports per-event
    # outcomes in the BODY. Checking only the status code reports success for a
    # batch that was rejected in full — which is exactly what happened: every
    # event failed timestamp validation while the CLI printed "5 case(s) → ...".
    # A push that silently lands nothing is worse than one that fails loudly.
    errors = _batch_errors(resp)
    if errors:
        raise RuntimeError(
            f"{len(errors)} of {len(events)} event(s) rejected: {errors[0]}")

    link = _trace_url(base, auth, run_name)
    return f"{len(suite.cases)} case(s) → {link or dataset_name(hub_dir)}"


def _trace_url(base: str, auth: str, run_name: str) -> str | None:
    """A URL that actually opens this run in the Langfuse UI.

    Langfuse UI routes are project-scoped (`/project/<id>/traces`), and the
    project id is not derivable from the OTLP endpoint — a bare
    `<host>/traces?tags=...` 404s. So resolve it once per push and hand the
    operator a link they can click, rather than a dataset name they then have
    to go hunting for.

    Best-effort: a failure here must not fail a push that already succeeded.
    """
    try:
        import httpx

        resp = httpx.get(f"{base}/api/public/projects",
                         headers={"Authorization": f"Basic {auth}"}, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            return None
        projects = resp.json().get("data") or []
        if not projects:
            return None
        pid = projects[0].get("id")
        if not pid:
            return None
        return f"{base}/project/{pid}/traces?tags={EVAL_TAG}"
    except Exception as exc:  # noqa: BLE001 — a link is a nicety, never a failure
        log.debug("could not resolve the langfuse project url: %s", exc)
        return None


def _batch_errors(resp) -> list[str]:
    """Per-event failures from an ingestion response body. [] when all landed.

    Tolerant of shape drift: a body we cannot parse is treated as "no reported
    errors" rather than a hard failure, since the status code already passed.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — best-effort parse; the status already passed
        return []
    if not isinstance(body, dict):
        return []
    out: list[str] = []
    for err in body.get("errors") or []:
        if not isinstance(err, dict):
            out.append(str(err)[:200])
            continue
        detail = err.get("message") or err.get("error") or "rejected"
        out.append(f"{err.get('id', '?')}: {str(detail)[:160]}")
    return out
