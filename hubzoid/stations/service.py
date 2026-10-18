"""Calling a station from a work-item workflow, its one retry, and handoffs.

A station call is identified by its work item, generation, stage and revision.
Its attempt runs as one workflow-engine step (`hz_station:<stage>`), so a
finished call is never run again on recovery, redrive or continuation. The call
pins the station definition it ran. Retries follow one rule: the orchestrator
may direct exactly one retry of a failed or malformed call, with a
clarification. A crash during an attempt is recovered at most once
(`recovery_attempts`), separately from that retry.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from .. import redaction
from ..work_items import config as config_lib
from ..work_items import store as wi_store
from ..work_items.errors import WorkItemError, not_found
from . import definitions

KIND_RE = r"^[a-z][a-z0-9-]{0,63}$"
_JSON = ("definition_json", "output_json", "attempts_json", "usage_json")


class StationError(WorkItemError):
    """A station call that did not produce a usable result. `call_id` names
    it for `hub.retry_station`."""

    def __init__(self, code: str, message: str, call_id: str, *, retryable: bool = True, **fields):
        super().__init__(code, message, status=409, call_id=call_id, retryable=retryable, **fields)
        self.call_id = call_id


class StationOutputError(StationError):
    """The station answered, but not in the shape its schema requires."""


# -- rows --------------------------------------------------------------------------

def _engine(hub_dir):
    return wi_store.engine(hub_dir)


def _decode(row):
    if row is None:
        return None
    out = dict(row)
    for col in _JSON:
        if out.get(col) is not None:
            out[col] = json.loads(out[col])
    return out


def get_call(hub_dir, call_id: str) -> dict | None:
    with _engine(hub_dir).connect() as c:
        return _decode(c.execute(text("SELECT * FROM hz_station_calls WHERE id=:i"), {"i": call_id}).mappings().first())


def _call_by_key(hub_dir, hub, item_id, generation, stage, revision_no) -> dict | None:
    with _engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_station_calls WHERE hub=:h AND item_id=:i AND generation=:g "
                             "AND stage=:s AND revision_no=:r"),
                        {"h": hub, "i": item_id, "g": generation, "s": stage, "r": revision_no}).mappings().first()
    return _decode(row)


def _update_call(hub_dir, call_id: str, *, expected_revision: int, **fields) -> dict:
    fields = {k: (wi_store.dumps(v) if k in _JSON and v is not None and not isinstance(v, str) else v)
              for k, v in fields.items()}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with _engine(hub_dir).begin() as c:
        changed = c.execute(text(f"UPDATE hz_station_calls SET {sets}, revision=revision+1, updated=:_t "
                                 "WHERE id=:_id AND revision=:_rev"),
                            dict(fields, _id=call_id, _rev=expected_revision, _t=time.time())).rowcount
    current = get_call(hub_dir, call_id)
    if not changed:
        from ..work_items.errors import revision_conflict
        raise revision_conflict(current["revision"] if current else 0)
    return current


def list_calls(hub_dir, item_id: str) -> list[dict]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT * FROM hz_station_calls WHERE item_id=:i ORDER BY created"),
                         {"i": item_id}).mappings().all()
    return [_decode(r) for r in rows]


def call_view(row: dict, *, full: bool) -> dict:
    out = {k: row.get(k) for k in ("id", "item_id", "generation", "stage", "revision_no", "agent",
                                   "definition_digest", "status", "error_code", "retry_used",
                                   "recovery_count", "revision", "created", "updated")}
    out["engine"] = (row.get("definition_json") or {}).get("engine")
    out["attempts"] = [{k: a.get(k) for k in ("n", "kind", "status", "started", "ended")}
                       for a in (row.get("attempts_json") or [])]
    if full:
        out["message"] = row.get("message")
        out["output"] = row.get("output_json")
        out["error"] = row.get("error")
        out["tools"] = (row.get("definition_json") or {}).get("tools")
        out["usage"] = row.get("usage_json")
    return out


# -- calling -----------------------------------------------------------------------

def _settings(hub_dir) -> dict:
    cfg = config_lib.load(hub_dir)
    return cfg.stations


def call(agent: str, message: str, *, stage: str, revision: int = 1, response_model=None):
    """Run station `agent` once for this work item's `stage`. Returns its
    validated output (a dict, or a `response_model` instance). Raises
    `StationOutputError` or `StationError` with the call id for the one retry."""
    from ..work_items.facade import _step, _work

    ctx, work = _work()
    item = work["item"]
    if not isinstance(stage, str) or not stage or len(stage) > 128:
        raise WorkItemError("invalid_request", "A station call needs a stage (1 to 128 characters).")
    stages = (item.get("route_json") or {}).get("stages")
    if stages is not None and stage not in stages:
        raise WorkItemError("scope_exceeded", f"Route {item['route']!r} does not include stage {stage!r}.",
                            status=403)
    if not isinstance(message, str) or not message.strip():
        raise WorkItemError("invalid_request", "A station call needs a message.")
    station = definitions.load(ctx.hub_dir, agent)
    schema = station.schema
    if response_model is not None:
        model_schema = response_model.model_json_schema()
        if schema is not None and schema != model_schema:
            raise WorkItemError("invalid_station_definition",
                                f"{agent} has a response_schema file and this call passes a different "
                                "response_model. Use one.", status=422)
        schema = model_schema
    pinned = dict(station.to_json(), schema=schema)
    digest = "sha256:" + hashlib.sha256(message.encode()).hexdigest()
    row = _call_by_key(ctx.hub_dir, item["hub"], item["id"], item["generation"], stage, revision)
    if row is None:
        from sqlalchemy.exc import IntegrityError

        t = time.time()
        try:
            with _engine(ctx.hub_dir).begin() as c:
                c.execute(text(
                    "INSERT INTO hz_station_calls (id, hub, item_id, generation, stage, revision_no, agent, "
                    "definition_digest, definition_json, message_digest, message, status, attempts_json, "
                    "revision, created, updated) VALUES (:id, :h, :i, :g, :s, :r, :a, :dd, :dj, :md, :m, "
                    "'started', '[]', 1, :t, :t)"),
                    {"id": str(uuid.uuid4()), "h": item["hub"], "i": item["id"], "g": item["generation"],
                     "s": stage, "r": revision, "a": agent, "dd": station.digest,
                     "dj": wi_store.dumps(pinned), "md": digest, "m": message, "t": t})
        except IntegrityError:
            pass
        row = _call_by_key(ctx.hub_dir, item["hub"], item["id"], item["generation"], stage, revision)
    if row["message_digest"] != digest or row["agent"] != agent:
        raise StationError("call_conflict", f"Stage {stage!r} already called {row['agent']} with a different "
                           "message. Use a new stage or revision for new work.", row["id"], retryable=False)
    call_id = row["id"]
    output = _step(f"hz_station:{stage}", lambda: _attempt(ctx, work, call_id, "initial"))
    return response_model.model_validate(output) if response_model is not None else output


def retry(call_id: str, clarification: str, *, response_model=None):
    """The orchestrator's one retry of a failed or malformed station call,
    with a clarification. A second retry is refused (`retry_exhausted`)."""
    from ..work_items.facade import _step, _work

    ctx, work = _work()
    if not isinstance(clarification, str) or not clarification.strip():
        raise WorkItemError("invalid_request", "A retry needs a clarification for the agent.")

    def claim():
        row = get_call(ctx.hub_dir, call_id)
        if row is None or row["item_id"] != work["item"]["id"]:
            raise not_found("station call")
        if row["status"] == "completed":
            return row["output_json"]
        if row["retry_used"] or _settings(ctx.hub_dir)["orchestrator_retries"] < 1:
            raise StationError("retry_exhausted", "This station call already used its one retry. A person "
                               "must correct the definition or the task.", call_id, retryable=False)
        if row["status"] not in ("failed",):
            raise StationError("call_conflict", f"The call is {row['status']}; only a failed call can be "
                               "retried.", call_id, retryable=False)
        _update_call(ctx.hub_dir, call_id, expected_revision=row["revision"], retry_used=1,
                     clarification=clarification[:20000], status="started")
        return None

    done = _step(f"hz_station_retry_claim:{call_id}", claim)
    if done is not None:
        return response_model.model_validate(done) if response_model is not None else done
    row = get_call(ctx.hub_dir, call_id)
    output = _step(f"hz_station_retry:{row['stage']}", lambda: _attempt(ctx, work, call_id, "retry"))
    return response_model.model_validate(output) if response_model is not None else output


def _attempt(ctx, work, call_id: str, kind: str):
    """One attempt, inside its engine step. Returns the validated output or
    raises; either way the call row records what happened."""
    from .. import runtime as runtime_lib
    from ..workflows import context

    hub_dir = ctx.hub_dir
    row = get_call(hub_dir, call_id)
    if row["status"] == "completed":
        return row["output_json"]
    settings = _settings(hub_dir)
    attempts = list(row.get("attempts_json") or [])
    recovery = row["recovery_count"]
    if attempts and attempts[-1].get("status") == "running":
        # The process stopped during the last attempt. Recover once, then stop.
        attempts[-1] = dict(attempts[-1], status="interrupted", ended=time.time())
        recovery += 1
        if recovery > settings["recovery_attempts"]:
            _update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=attempts,
                         recovery_count=recovery, status="failed", error_code="station_interrupted",
                         error="The station was interrupted more often than its recovery allows.")
            raise StationError("station_interrupted", "The station was interrupted and has no recovery left. "
                               "Inspect it, then retry or restart the work.", call_id)
        kind = "recovery"
    attempts.append({"n": len(attempts) + 1, "kind": kind, "status": "running", "started": time.time()})
    row = _update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=attempts,
                       recovery_count=recovery, status="started")
    definition = row["definition_json"]
    message = row["message"]
    if row.get("clarification") and kind in ("retry", "recovery") and row["retry_used"]:
        message += ("\n\nYour previous answer could not be used. " + row["clarification"])
    identity = context._identity(ctx)
    previous = work.get("station_call")
    work["station_call"] = call_id
    try:
        result = runtime_lib.run_station(hub_dir, definition, message, subject=identity.subject,
                                         timeout=definition.get("timeout_seconds") or settings.get("timeout_seconds"))
    except runtime_lib.AgentRunError as exc:
        _finish(hub_dir, call_id, "failed", error_code="station_failed", error=redaction.scrub_error(exc))
        raise StationError("station_failed", redaction.scrub_text(str(exc)), call_id) from None
    finally:
        work["station_call"] = previous
    output = result.get("output")
    schema = definition.get("schema")
    problems = _schema_problems(schema, output) if schema is not None else []
    if schema is None and output is None:
        output = {"text": result.get("text") or ""}
    if problems:
        _finish(hub_dir, call_id, "failed", error_code="station_output_error",
                error="; ".join(problems)[:4000], usage=result.get("usage"),
                text=redaction.scrub_text(result.get("text") or "")[:4000])
        raise StationOutputError("station_output_error",
                                 "The station's answer does not match its schema: " + "; ".join(problems)[:1000],
                                 call_id, problems=problems[:20])
    _finish(hub_dir, call_id, "completed", output=output, usage=dict(result.get("usage") or {},
                                                                       model=result.get("model")))
    return output


def _finish(hub_dir, call_id, status, *, output=None, error_code=None, error=None, usage=None, text=None):
    for _ in range(10):
        row = get_call(hub_dir, call_id)
        attempts = list(row.get("attempts_json") or [])
        if attempts:
            attempts[-1] = dict(attempts[-1], status=status, ended=time.time())
        fields = dict(status=status, attempts_json=attempts, error_code=error_code,
                      error=error if error is None else (error + (f"\nAnswer: {text}" if text else "")),
                      usage_json=usage)
        if output is not None:
            fields["output_json"] = output
        try:
            return _update_call(hub_dir, call_id, expected_revision=row["revision"], **fields)
        except WorkItemError:
            continue


def _schema_problems(schema: dict, value) -> list[str]:
    import jsonschema

    if value is None:
        return ["the answer contained no JSON object"]
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))][:20]


# -- handoffs ----------------------------------------------------------------------

def _handoff_settings(hub_dir) -> dict:
    return config_lib.load(hub_dir).handoffs


def save_handoff_here(kind, title, markdown) -> dict:
    """`save_handoff` inside a running station: stored for this work item."""
    from ..work_items.facade import _work

    ctx, work = _work()
    return save_handoff(ctx.hub_dir, work["item"], kind, title, markdown, call_id=work.get("station_call"),
                        created_by=work["item"].get("requester") or "")


def save_handoff(hub_dir, item: dict, kind, title, markdown, *, call_id=None, created_by="") -> dict:
    import re

    from sqlalchemy.exc import IntegrityError

    settings = _handoff_settings(hub_dir)
    if not isinstance(kind, str) or not re.match(KIND_RE, kind):
        raise WorkItemError("validation_failed", "kind is a lowercase slug, such as analysis.", status=422)
    if settings["kinds"] is not None and kind not in settings["kinds"]:
        raise WorkItemError("validation_failed", f"kind must be one of {', '.join(settings['kinds'])}.",
                            status=422)
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        raise WorkItemError("validation_failed", "title is 1 to 200 characters.", status=422)
    if not isinstance(markdown, str) or not markdown:
        raise WorkItemError("validation_failed", "markdown is required.", status=422)
    if len(markdown) > settings["max_characters"] or len(markdown.encode()) > 800000:
        raise WorkItemError("payload_too_large", f"A handoff is at most {settings['max_characters']} "
                            "characters.", status=413)
    sha = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
    producer = hashlib.sha256(json.dumps([call_id, kind, title, sha]).encode()).hexdigest()
    doc_id = str(uuid.uuid4())
    try:
        with _engine(hub_dir).begin() as c:
            c.execute(text("INSERT INTO hz_handoffs (id, hub, item_id, generation, call_id, producer_key, kind, "
                           "title, markdown, sha256, characters, created_by, created) VALUES (:id, :h, :i, :g, "
                           ":c, :p, :k, :t, :m, :s, :n, :by, :at)"),
                      {"id": doc_id, "h": item["hub"], "i": item["id"], "g": item["generation"], "c": call_id,
                       "p": producer, "k": kind, "t": title, "m": markdown, "s": sha, "n": len(markdown),
                       "by": created_by or "", "at": time.time()})
    except IntegrityError:
        pass   # the same save replayed: return the original
    with _engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT id, kind, title, sha256, characters FROM hz_handoffs WHERE hub=:h AND "
                             "item_id=:i AND producer_key=:p"),
                        {"h": item["hub"], "i": item["id"], "p": producer}).mappings().first()
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "sha256": row["sha256"],
            "character_count": row["characters"]}


def read_handoff_here(doc_id) -> dict:
    from ..work_items.facade import _work

    ctx, work = _work()
    return read_handoff(ctx.hub_dir, work["item"]["id"], doc_id)


def read_handoff(hub_dir, item_id: str, doc_id) -> dict:
    """A handoff of this work item. Any other id reads as not found."""
    if not isinstance(doc_id, str):
        raise not_found("handoff")
    with _engine(hub_dir).connect() as c:
        row = c.execute(text("SELECT * FROM hz_handoffs WHERE id=:d AND item_id=:i"),
                        {"d": doc_id, "i": item_id}).mappings().first()
    if row is None:
        raise not_found("handoff")
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "sha256": row["sha256"],
            "character_count": row["characters"], "generation": row["generation"], "call_id": row["call_id"],
            "markdown": row["markdown"]}


def list_handoffs(hub_dir, item_id: str) -> list[dict]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text("SELECT id, kind, title, sha256, characters, generation, call_id, created FROM "
                              "hz_handoffs WHERE item_id=:i ORDER BY created"), {"i": item_id}).mappings().all()
    return [dict(r) for r in rows]


def view_for(hub_dir, item_id: str, viewer: str) -> dict:
    """Station calls and handoffs of a work item, for `viewer`: everything for
    the person who asked for the work, state only for a manager."""
    from ..work_items import service as wi_service

    item = wi_store.require_item(hub_dir, item_id, Path(hub_dir).name.lower())
    visible, full = wi_service._may_see(hub_dir, item, viewer)
    if not visible:
        raise not_found()
    handoffs = list_handoffs(hub_dir, item_id)
    return {"calls": [call_view(r, full=full) for r in list_calls(hub_dir, item_id)],
            "handoffs": handoffs if full else [{k: h[k] for k in ("id", "kind", "created")} for h in handoffs]}
