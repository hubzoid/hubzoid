"""Calling a station from a work-item workflow, its one retry, and handoffs.

A station call is identified by its work item, generation, stage and revision.
Its attempt runs as one workflow-engine step (`hz_station:<stage>:r<revision>`), so a
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


def step_name(stage: str, revision: int, *, retry: bool = False) -> str:
    """The engine step that holds one attempt of the call for `stage`."""
    return f"hz_station{'_retry' if retry else ''}:{stage}:r{revision}"


def call(agent: str, message: str, *, stage: str, revision: int = 1, response_model=None, timeout=None,
         checkout: dict | None = None):
    """Run station `agent` once for this work item's `stage`. Returns its
    validated output (a dict, or a `response_model` instance). Raises
    `StationOutputError` or `StationError` with the call id for the one retry.
    `timeout` (seconds) may lower the station's time limit, never raise it.
    `checkout` ({url, ref}) copies a repository into the station's sandbox,
    cloned with the host's credentials, which never enter the sandbox."""
    from ..work_items.facade import _step, _work
    from ..workflows import context

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
    digest = "sha256:" + hashlib.sha256(message.encode()).hexdigest()
    # A saved call keeps the definition it pinned, even if the files changed
    # or were removed since: only a new call reads the current files.
    row = _call_by_key(ctx.hub_dir, item["hub"], item["id"], item["generation"], stage, revision)
    if row is None:
        _create(ctx.hub_dir, item, agent, message, digest, stage, revision, response_model, timeout, checkout)
        row = _call_by_key(ctx.hub_dir, item["hub"], item["id"], item["generation"], stage, revision)
    if row["message_digest"] != digest or row["agent"] != agent:
        raise StationError("call_conflict", f"Stage {stage!r} already called {row['agent']} with a different "
                           "message. Use a new stage or revision for new work.", row["id"], retryable=False)
    call_id = row["id"]
    if row["status"] != "completed":
        context._recheck(ctx, "A station call")
    output = _step(step_name(stage, revision), lambda: _attempt(ctx, work, call_id, "initial"))
    return response_model.model_validate(output) if response_model is not None else output


def _create(hub_dir, item, agent, message, digest, stage, revision, response_model, timeout, checkout) -> None:
    from sqlalchemy.exc import IntegrityError

    from . import tools as station_tools

    station = definitions.load(hub_dir, agent)
    profile = None
    if station.sandbox:
        from ..sandbox import config as sandbox_config

        try:
            profile = sandbox_config.load_profile(hub_dir, station.sandbox)
            sandbox_config.check_profile(hub_dir, profile)
        except WorkItemError as exc:
            raise WorkItemError("sandbox_unavailable", f"{agent}'s sandbox cannot be used: {exc.message} "
                                "Hubzoid never falls back to the host shell.", status=503,
                                retryable=False) from None
    elif checkout:
        raise WorkItemError("invalid_request", f"{agent} has no sandbox to check a repository out into.",
                            status=422)
    if checkout is not None and (not isinstance(checkout, dict) or not isinstance(checkout.get("url"), str)
                                 or set(checkout) - {"url", "ref", "dest", "depth", "commit"}
                                 or (checkout.get("depth") is not None
                                     and (isinstance(checkout["depth"], bool) or not isinstance(checkout["depth"], int)
                                          or not 1 <= checkout["depth"] <= 10000))):
        raise WorkItemError("invalid_request", "checkout is {url, ref, depth, commit} (all but url optional; depth 1 "
                            "to 10000 for a shallow clone; commit pins the checkout to an exact commit).")
    schema = station.schema
    if response_model is not None:
        model_schema = response_model.model_json_schema()
        if schema is not None and schema != model_schema:
            raise WorkItemError("invalid_station_definition",
                                f"{agent} has a response_schema file and this call passes a different "
                                "response_model. Use one.", status=422)
        schema = model_schema
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 0 < timeout <= 86400):
        raise WorkItemError("invalid_request", "timeout is a number of seconds from 1 to 86400.")
    pinned = dict(station.to_json(), schema=schema, call_timeout=timeout,
                  sandbox_profile=profile.to_json() if profile else None,
                  checkout=_clean_checkout(checkout),
                  tools_digest=station_tools.digest(station_tools.for_station(hub_dir, station.tools,
                                                                               sandbox=bool(profile))))
    t = time.time()
    try:
        with _engine(hub_dir).begin() as c:
            c.execute(text(
                "INSERT INTO hz_station_calls (id, hub, item_id, generation, stage, revision_no, agent, "
                "definition_digest, definition_json, message_digest, message, status, attempts_json, "
                "revision, created, updated) VALUES (:id, :h, :i, :g, :s, :r, :a, :dd, :dj, :md, :m, "
                "'started', '[]', 1, :t, :t)"),
                {"id": str(uuid.uuid4()), "h": item["hub"], "i": item["id"], "g": item["generation"],
                 "s": stage, "r": revision, "a": agent, "dd": station.digest,
                 "dj": wi_store.dumps(pinned), "md": digest, "m": message, "t": t})
    except IntegrityError:
        pass   # created concurrently: the caller reads the existing row


def _clean_checkout(checkout: dict | None) -> dict | None:
    """The checkout as recorded: the URL without any credential in it. The
    clone itself uses the host's Git credentials."""
    if not checkout:
        return None
    from ..sandbox.service import plain_url

    return dict(checkout, url=plain_url(checkout["url"]))


def retry(call_id: str, clarification: str, *, response_model=None):
    """The orchestrator's one retry of a failed or malformed station call,
    with a clarification. A second retry is refused (`retry_exhausted`)."""
    from ..work_items.facade import _step, _work
    from ..workflows import context

    ctx, work = _work()
    if not isinstance(clarification, str) or not clarification.strip():
        raise WorkItemError("invalid_request", "A retry needs a clarification for the agent.")
    clarification = clarification[:20000]

    def claim():
        row = get_call(ctx.hub_dir, call_id)
        if row is None or row["item_id"] != work["item"]["id"]:
            raise not_found("station call")
        if row["status"] == "completed":
            return row["output_json"]
        if row["retry_used"] and row["status"] == "started" and row.get("clarification") == clarification:
            # This same claim committed before the engine saved its step.
            return None
        return _claim_retry(ctx.hub_dir, row, clarification, expected_revision=row["revision"])

    done = _step(f"hz_station_retry_claim:{call_id}", claim)
    if done is not None:
        return response_model.model_validate(done) if response_model is not None else done
    row = get_call(ctx.hub_dir, call_id)
    context._recheck(ctx, "A station retry")
    output = _step(step_name(row["stage"], row["revision_no"], retry=True),
                   lambda: _attempt(ctx, work, call_id, "retry"))
    return response_model.model_validate(output) if response_model is not None else output


def _claim_retry(hub_dir, row: dict, clarification: str, *, expected_revision: int, by: str | None = None):
    """Spend the call's one retry, by compare-and-set on its revision. The
    orchestrator and an operator share this allowance."""
    if row["retry_used"] or _settings(hub_dir)["orchestrator_retries"] < 1:
        raise StationError("retry_exhausted", "This station call already used its one retry. A person "
                           "must correct the definition or the task.", row["id"], retryable=False)
    if row["status"] != "failed":
        raise StationError("call_conflict", f"The call is {row['status']}; only a failed call can be "
                           "retried.", row["id"], retryable=False)
    if expected_revision != row["revision"]:
        from ..work_items.errors import revision_conflict
        raise revision_conflict(row["revision"])
    _update_call(hub_dir, row["id"], expected_revision=expected_revision, retry_used=1,
                 clarification=clarification, status="started", error_code=None, error=None)
    return None


def operator_retry(hub_dir, item_id: str, call_id: str, actor: str, *, clarification: str,
                   expected_revision: int | None = None, surface: str | None = None) -> dict:
    """An operator directs the call's one retry after its workflow stopped on
    the failure without using it. The work continues from that station call."""
    from ..work_items import engine as engine_lib
    from ..work_items import service as wi_service

    item = wi_store.require_item(hub_dir, item_id, Path(hub_dir).name.lower())
    if not (wi_service._is_requester(item, actor) or wi_service._can(hub_dir, actor, "workflows_manage")):
        raise WorkItemError("permission_denied", "Only the person who asked for this work, or a manager, can "
                            "retry its station call.", status=403)
    row = get_call(hub_dir, call_id)
    if row is None or row["item_id"] != item_id:
        raise not_found("station call")
    if item["state"] not in ("needs_input", "failed", "stopped"):
        raise StationError("call_conflict", "The work is still running, so its workflow decides about the "
                           "retry. Wait until it stops.", call_id, retryable=False)
    if not isinstance(clarification, str) or not clarification.strip():
        raise WorkItemError("validation_failed", "Say what the agent should do differently.", status=422)
    unresolved = [o for o in wi_store.list_ops(hub_dir, item["hub"], item_id=item_id) if wi_store.unresolved(o)]
    if unresolved:
        raise WorkItemError("operation_unresolved", "An operation's outcome is still unknown. Resolve it "
                            "before retrying.", status=409, operation_id=unresolved[0]["id"])
    name = step_name(row["stage"], row["revision_no"])
    with engine_lib.live(hub_dir) as eng:
        steps = [s if isinstance(s, dict) else vars(s) for s in (eng.steps(item["workflow_id"]) or [])]
    step = next((s.get("function_id") for s in steps if s.get("function_name") == name), None)
    if step is None:
        raise WorkItemError("stage_missing", "This execution has no saved step for that station call, so it "
                            "cannot continue from it. Restart the work instead.", status=409)
    _claim_retry(hub_dir, row, clarification.strip()[:20000],
                 expected_revision=row["revision"] if expected_revision is None else expected_revision)
    if item["state"] in ("failed", "stopped"):
        item = wi_store.update_item(hub_dir, item_id, expected_revision=item["revision"],
                                    state="needs_input", reason="station_retry")
    wi_service._continue(hub_dir, item, step, new_version=True)
    wi_service._audit(hub_dir, actor, "station_retry", item_id, surface=surface)
    return call_view(get_call(hub_dir, call_id), full=True)


def _attempt(ctx, work, call_id: str, kind: str):
    """One attempt, inside its engine step. Returns the validated output or
    raises; either way the call row records what happened."""
    from .. import runtime as runtime_lib
    from ..workflows import context
    from . import tools as station_tools

    hub_dir = ctx.hub_dir
    row = get_call(hub_dir, call_id)
    if row["status"] == "completed":
        return row["output_json"]
    if row["status"] in ("failed", "cancelled"):
        # Already decided, and the engine lost the step's result: the same
        # outcome again. Only a claimed retry starts another attempt.
        raise _stored_error(row)
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
    elif row["retry_used"] and not any(a.get("kind") == "retry" for a in attempts):
        kind = "retry"      # an operator's retry, continuing from this step
    definition = row["definition_json"]
    limit = _time_limit(definition, settings)
    spent = sum(max(0.0, (a.get("ended") or a["started"]) - a["started"]) for a in attempts)
    if limit is not None and spent >= limit:
        _finish(hub_dir, call_id, "failed", error_code="station_timeout",
                error=f"The station used its {limit:g} seconds of active time.")
        raise StationError("station_timeout", "The station used all of its active time.", call_id)
    boxed = bool(definition.get("sandbox"))
    registry = station_tools.for_station(hub_dir, definition.get("tools") or (), sandbox=boxed)
    if station_tools.digest(registry) != definition.get("tools_digest"):
        _finish(hub_dir, call_id, "failed", error_code="definition_changed",
                error="A tool this station lists changed after the call started.")
        raise StationError("definition_changed", "A tool this station lists changed since the call started, "
                           "so the call cannot run again as pinned. Restore the tool, or start a new stage "
                           "revision.", call_id, retryable=False)
    attempts.append({"n": len(attempts) + 1, "kind": kind, "status": "running", "started": time.time()})
    row = _update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=attempts,
                       recovery_count=recovery, status="started")
    sandbox_id = None
    if boxed:
        sandbox_id = _station_sandbox(hub_dir, work, row, kind)
        attempts[-1]["sandbox_id"] = sandbox_id
        row = _update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=attempts)
        registry = station_tools.for_station(hub_dir, definition.get("tools") or (), sandbox=True,
                                             sandbox_id=sandbox_id)
    message = row["message"]
    if row.get("clarification") and row["retry_used"] and kind in ("retry", "recovery"):
        message += ("\n\nYour previous answer could not be used. " + row["clarification"])
    identity = context._identity(ctx)
    previous = work.get("station_call")
    work["station_call"] = call_id
    try:
        result = runtime_lib.run_station(hub_dir, definition, message, subject=identity.subject,
                                         timeout=None if limit is None else limit - spent,
                                         registry=station_tools.guarded(registry, ctx, work))
    except WorkItemError as exc:       # refused before it ran, such as a usage limit
        _finish(hub_dir, call_id, "failed", error_code=exc.code, error=exc.message[:4000])
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationError(exc.code, exc.message, call_id, retryable=False) from None
    except runtime_lib.StationTimeout as exc:
        _finish(hub_dir, call_id, "failed", error_code="station_timeout", error=redaction.scrub_error(exc))
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationError("station_timeout", "The station ran out of time.", call_id) from None
    except runtime_lib.AgentRunError as exc:
        _finish(hub_dir, call_id, "failed", error_code="station_failed", error=redaction.scrub_error(exc))
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationError("station_failed", redaction.scrub_text(str(exc)), call_id) from None
    finally:
        work["station_call"] = previous
    context._owned(ctx)      # a process that lost the hub commits nothing
    if _cancelled(hub_dir, work["item"]["id"]):
        _finish(hub_dir, call_id, "cancelled", error_code="cancelled", error="The work was cancelled.")
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationError("cancelled", "The work was cancelled while the station ran.", call_id,
                           retryable=False)
    output = result.get("output")
    schema = definition.get("schema")
    problems = _schema_problems(schema, output) if schema is not None else []
    if schema is None and output is None:
        output = {"text": result.get("text") or ""}
    if problems:
        _finish(hub_dir, call_id, "failed", error_code="station_output_error",
                error="; ".join(problems)[:4000], usage=result.get("usage"),
                text=redaction.scrub_text(result.get("text") or "")[:4000])
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationOutputError("station_output_error",
                                 "The station's answer does not match its schema: " + "; ".join(problems)[:1000],
                                 call_id, problems=problems[:20])
    if sandbox_id is not None:
        _export(hub_dir, sandbox_id, work["item"]["id"], call_id)
    _finish(hub_dir, call_id, "completed", output=output, usage=dict(result.get("usage") or {},
                                                                       model=result.get("model")))
    if sandbox_id is not None:
        from ..sandbox import service as sandbox_service
        sandbox_service.release(hub_dir, sandbox_id, "completed")
    return output


def _station_sandbox(hub_dir, work, row: dict, kind: str) -> str:
    """The call's sandbox, ready. A crash recovery reattaches the same one; if
    it is gone (disappeared, removed by an operator or expired) the call stops
    with `sandbox_lost` instead of starting empty. Only an explicit retry gets
    a fresh sandbox, from the same checkout, and only when the old one is gone.
    A first allocation that never became ready (nothing ran in it) is simply
    made again."""
    from ..sandbox import service as sandbox_service

    definition = row["definition_json"]
    history = sandbox_service.list_rows(hub_dir, owner_kind="station_call", owner_id=row["id"], limit=10)
    live = [b for b in history if b.get("live_key")]
    try:
        if live and live[0]["state"] in ("ready", "stopped"):
            try:
                return sandbox_service.attach(hub_dir, live[0]["id"])["id"]
            except WorkItemError as exc:
                if exc.code != "sandbox_lost" or kind != "retry":
                    raise
        elif not live and kind != "retry" and any(
                b["state"] not in ("start_failed", "setup_failed") for b in history):
            raise sandbox_service._lost(history[0])
        box = sandbox_service.allocate(
            hub_dir, owner_kind="station_call", owner_id=row["id"], profile_id=definition["sandbox"],
            item_id=row["item_id"], actor=work["item"].get("requester") or "", checkout=definition.get("checkout"),
            profile_json=definition.get("sandbox_profile"))
    except WorkItemError as exc:
        _finish(hub_dir, row["id"], "failed", error_code=exc.code, error=exc.message[:4000])
        raise StationError(exc.code, exc.message, row["id"], retryable=exc.code != "sandbox_lost",
                           sandbox_id=exc.fields.get("sandbox_id")) from None
    return box["id"]


def _keep_or_release(hub_dir, sandbox_id: str | None, call_id: str) -> None:
    """After a failed attempt: keep the sandbox (stopped, files intact) while
    the call's one retry is still possible, otherwise remove it."""
    if sandbox_id is None:
        return
    from ..sandbox import service as sandbox_service

    row = get_call(hub_dir, call_id)
    try:
        if row["status"] == "failed" and not row["retry_used"] and _settings(hub_dir)["orchestrator_retries"] > 0:
            sandbox_service.stop(hub_dir, sandbox_id, reason="awaiting_retry")
        else:
            sandbox_service.release(hub_dir, sandbox_id, "failed" if row["status"] == "failed" else row["status"])
    except WorkItemError:
        pass   # the reaper finishes it


def _export(hub_dir, sandbox_id: str, item_id: str, call_id: str) -> None:
    """Each checkout's new commits as a patch in the hub's output."""
    from .. import datadir
    from ..sandbox import service as sandbox_service

    box = sandbox_service.get(hub_dir, sandbox_id)
    if not box or not box.get("checkouts_json"):
        return
    try:
        sandbox_service.export_patches(hub_dir, sandbox_id,
                                       datadir.resolve(hub_dir, Path("output") / "work-items" / item_id / call_id))
    except WorkItemError as exc:
        code = exc.code if exc.code == "sandbox_lost" else "export_failed"
        _finish(hub_dir, call_id, "failed", error_code=code, error=exc.message[:4000])
        _keep_or_release(hub_dir, sandbox_id, call_id)
        raise StationError(code, exc.message, call_id) from None


def _time_limit(definition: dict, settings: dict):
    """Active seconds across all attempts: the strictest of the hub setting,
    the station and the call. None means no limit (the default)."""
    limits = [v for v in (settings.get("timeout_seconds"), definition.get("timeout_seconds"),
                          definition.get("call_timeout")) if v is not None]
    return min(limits) if limits else None


def _cancelled(hub_dir, item_id: str) -> bool:
    item = wi_store.get_item(hub_dir, item_id)
    return bool(item and (item.get("cancel_requested") or item["state"] == "cancelled"))


def _stored_error(row: dict) -> StationError:
    code = row.get("error_code") or "station_failed"
    message = (row.get("error") or "The station call failed.").split("\nAnswer:")[0][:1000]
    cls = StationOutputError if code == "station_output_error" else StationError
    return cls(code, message, row["id"], retryable=code not in ("cancelled", "definition_changed"))


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
    calls = []
    for r in list_calls(hub_dir, item_id):
        out = call_view(r, full=full)
        if (r.get("definition_json") or {}).get("sandbox"):
            from ..sandbox import service as sandbox_service
            boxes = [sandbox_service.view(b) for b in sandbox_service.list_rows(
                hub_dir, owner_kind="station_call", owner_id=r["id"], limit=10)]
            out["sandboxes"] = boxes if full else [{k: b[k] for k in ("id", "state", "end_reason", "profile_id")}
                                                   for b in boxes]
        calls.append(out)
    return {"calls": calls,
            "handoffs": handoffs if full else [{k: h[k] for k in ("id", "kind", "created")} for h in handoffs]}
