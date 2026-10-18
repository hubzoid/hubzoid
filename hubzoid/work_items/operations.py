"""`hub.operation.run`: one recorded, recoverable external write.

The protocol, the same for every runtime and adapter:

1. Decide the write mode. `off` refuses before anything is recorded. `draft`
   calls the adapter's real draft function instead of sending.
2. Record the exact intent first: a logical key (never a workflow or attempt
   id), the canonical arguments and their `sha256:` digest. The same key with
   different arguments is refused.
3. In `approve` mode, park until a person approves that exact digest. Before
   sending, check the digest, the approver's current access and, when the
   adapter can read it, the target's version again.
4. Mark the attempt `sending` and commit, then call the adapter.
5. Record `succeeded`, `failed_before_send` (the adapter raised `NotSent`),
   `rejected` (`Rejected`) or `ambiguous`.

An interrupted `sending` is recovered by the operation's class:

* ``idempotent``: the same request is safe to send again.
* ``provider_key``: send again with the same provider key, only while the
  provider still honours it (`provider_key_ttl`).
* ``reconcile``: ask the read-only `reconcile` check. It must say `applied`
  (with the result), `safe_to_retry` (proof it never happened) or `unknown`.
* ``escalate``: never guess. The operation becomes `ambiguous`.

An ambiguous operation stops the work. Only the person who asked for the work
decides: done (with evidence), retry (accepting the risk of a duplicate) or
abandon. An empty search result is never proof that something was not sent.

The pattern follows Inbox Zero's durable email send (a payload hash under a
unique mutation id, and an expired processing lease read as uncertain, never as
"send again"), extended with recovery classes and exact approval.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any, Callable

from .. import redaction
from . import config as config_lib
from . import store
from .errors import NotSent, Parked, Rejected, WorkItemError

RECOVERY = ("idempotent", "provider_key", "reconcile", "escalate")
APPLIED, SAFE_TO_RETRY, UNKNOWN = "applied", "safe_to_retry", "unknown"


class Reconciled:
    """What a `reconcile` check returns."""

    def __init__(self, status: str, result: Any = None, evidence: Any = None):
        if status not in (APPLIED, SAFE_TO_RETRY, UNKNOWN):
            raise ValueError("reconcile must return applied, safe_to_retry or unknown")
        self.status, self.result, self.evidence = status, result, evidence


def _canon(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Operation arguments cannot contain NaN or infinity.")
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"Operation arguments must be JSON values, not {type(value).__name__}.")


def canonical(value) -> bytes:
    """Sorted keys, no extra whitespace, arrays in order, `null` kept."""
    return json.dumps(_canon(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def logical_key(parts) -> str:
    return hashlib.sha256(canonical(list(parts))).hexdigest()


class OperationFailed(Exception):
    """The operation definitely did not happen: refused by the other system,
    abandoned or denied by a person. `code` says which."""

    def __init__(self, code: str, message: str, operation_id: str):
        super().__init__(message)
        self.code, self.message, self.operation_id = code, message, operation_id


def _resume_step() -> int | None:
    """The step a continuation must start from: this step when called inside
    a step (it re-runs), else the next step of the workflow."""
    try:
        from dbos._context import get_local_dbos_context
    except ImportError:  # pragma: no cover - DBOS is a required dependency
        return None
    ctx = get_local_dbos_context()
    if ctx is None or not ctx.workflow_id:
        return None
    if ctx.curr_step_function_id >= 0:
        return ctx.curr_step_function_id
    return ctx.function_id + 1


def _approver_still_allowed(hub_dir, hub: str, subject: str) -> bool:
    """The approver is still a usable account with access to this hub: the
    same check a run's own account passes (`workflows.identity.check`)."""
    from ..workflows.identity import IdentityError, check

    if not subject:
        return False
    try:
        check(hub_dir, hub, subject, "approver", what="This approval")
        return True
    except IdentityError:
        return False
    except Exception:  # noqa: BLE001 - unreadable access data is not an approval
        return False


def run(*, name: str, target: dict, arguments: dict, recovery: str,
        dispatch: Callable, reconcile: Callable | None = None, actions=None,
        channel: str | None = None, draft: Callable | None = None,
        target_version: Callable | None = None, provider_key_ttl: float | None = None,
        key: str | None = None) -> Any:
    """Perform one external write, recorded and recoverable.

    `dispatch(arguments)` sends; `draft(arguments)` saves a real draft; both
    return a JSON-serializable result. With `recovery="provider_key"` they are
    called as `fn(arguments, provider_key=...)`. `actions` names the actions
    this write performs: inside a work item it is required and must be allowed
    by the item's route, and a consequential action (email.send, erp.submit,
    customer_reply.send or one a hub registered) follows its write mode.
    `channel` is the channel it goes out on. `key` distinguishes deliberate
    repeats of the same name and target within one stage."""
    from ..workflows import context

    ctx = context._ctx()
    if recovery not in RECOVERY:
        raise ValueError(f"recovery must be one of {', '.join(RECOVERY)}")
    if recovery == "reconcile" and reconcile is None:
        raise WorkItemError("invalid_definition",
                            f"Operation {name!r} uses recovery 'reconcile' but has no reconcile check.",
                            status=422)
    if recovery == "provider_key" and not provider_key_ttl:
        raise WorkItemError("invalid_definition",
                            f"Operation {name!r} uses recovery 'provider_key' but declares no "
                            "provider_key_ttl (how long the provider honours the key).", status=422)
    if not isinstance(target, dict) or not isinstance(arguments, dict):
        raise ValueError("target and arguments must be JSON objects")
    hub_dir, hub = ctx.hub_dir, ctx.hub
    work = ctx.work or {}
    item = work.get("item")
    stage = work.get("stage")
    actions = [actions] if isinstance(actions, str) else list(actions or [])
    for action in actions:
        if not config_lib.ACTION_RE.match(action):
            raise WorkItemError("invalid_definition",
                                f"{action!r} is not an action id like 'email.send'.", status=422)
    if item is not None:
        if not actions:
            raise WorkItemError(
                "invalid_definition",
                f"Operation {name!r} must name the action it performs (actions=...), so the work "
                "item's route and the hub's write modes can govern it.", status=422)
        allowed = (item.get("route_json") or {}).get("actions")
        for action in actions:
            if allowed is not None and action not in allowed:
                raise WorkItemError(
                    "scope_exceeded",
                    f"Route {item['route']!r} does not allow {action}. A wider route needs a "
                    "new request from someone allowed to ask for it.", status=403)
    mode, fn = "direct", dispatch
    if actions:
        mode = config_lib.mode_for(config_lib.load(hub_dir), actions, channel)
        if mode == "off":
            raise WorkItemError(
                "write_mode_off",
                f"{', '.join(actions)} is off for this hub. Set it to draft or approve in "
                "work-items.yaml when the system is connected.", status=403)
        if mode == "draft":
            if draft is None:
                raise WorkItemError(
                    "draft_unsupported",
                    f"{', '.join(actions)} is set to draft, but {name!r} has no real draft in "
                    "its target system. Set the action to off or approve.", status=422)
            fn = draft
    if mode == "approve" and item is None:
        raise WorkItemError(
            "approval_needs_work_item",
            f"{', '.join(actions)} needs a person's approval, which only work items can wait for. "
            "Declare the workflow with work_items=... and start it as a work item.", status=422)
    if item is not None:
        parts = [hub, "item", item["id"], item["generation"], stage, name, target, key]
        requester = item.get("requester") or context._identity(ctx).subject
    else:
        source = (ctx.event or {}).get("id") or ctx.run_id or ""
        parts = [hub, "run", source, stage, name, target, key]
        requester = context._identity(ctx).subject
    lkey = logical_key(parts)
    payload = {"name": name, "target": target, "arguments": arguments, "mode": mode,
               "actions": sorted(actions)}
    args_digest = digest(payload)
    from .. import secretbox

    row, created = store.insert_op(
        hub_dir, id=str(uuid.uuid4()), hub=hub, logical_key=lkey,
        item_id=item["id"] if item else None, generation=item["generation"] if item else None,
        stage=stage, name=name, action=",".join(sorted(actions)) if actions else None, mode=mode,
        recovery=recovery, target_json=target, args_digest=args_digest,
        payload_enc=secretbox.encrypt_json(hub_dir, payload),
        state="awaiting_approval" if mode == "approve" else "pending",
        attempts_json=[], requester=requester or "", workflow=ctx.workflow,
        workflow_id=ctx.run_id or None,
        provider_key=uuid.uuid4().hex if recovery == "provider_key" else None,
        provider_key_expires=(time.time() + provider_key_ttl) if recovery == "provider_key" else None)
    if not created and row["args_digest"] != args_digest:
        raise WorkItemError(
            "operation_argument_conflict",
            f"Operation {name!r} was already recorded for this target with different content. "
            "Change the stage revision or pass a new key for a genuinely different action.",
            status=409, operation_id=row["id"])
    return _drive(ctx, row, fn, arguments, reconcile, target_version)


_UNKNOWN = ("We cannot confirm whether this action completed. The person who asked for this "
            "work must decide: done, retry or abandon.")


def _park(ctx, row, kind: str, message: str, reason: str | None = None):
    """Wait for a person. Only a work item can wait: outside one, an
    uncertain outcome stops the run and stays recorded for a person."""
    if not (ctx.work or {}).get("item"):
        raise OperationFailed("operation_ambiguous" if kind == "ambiguous" else "approval_needs_work_item",
                              message, row["id"])
    step = _resume_step()
    store.update_op(ctx.hub_dir, row["id"], resume_step=step, workflow_id=ctx.run_id or row["workflow_id"])
    raise Parked(kind, row["id"], message, reason=reason)


def _drive(ctx, row, fn, arguments, reconcile, target_version):
    hub_dir = ctx.hub_dir
    while True:
        state = row["state"]
        if state in ("succeeded", "resolved_done"):
            return row["result_json"]
        if state == "rejected":
            raise OperationFailed("operation_rejected", row.get("error") or "The other system refused it.", row["id"])
        if state == "abandoned":
            res = row.get("resolution_json") or {}
            code = "approval_denied" if res.get("decision") == "deny" else "operation_abandoned"
            raise OperationFailed(code, "A person stopped this operation.", row["id"])
        if state == "ambiguous":
            _park(ctx, row, "ambiguous", _UNKNOWN)
        if state == "sending":
            resend, row = _recover(ctx, row, reconcile)
            if not resend:                    # settled by the check, or now ambiguous
                continue
            # The earlier attempt may be resent: the same gate as a first send.
            row = _gate(ctx, row, target_version)
            row = _claim(hub_dir, row, (row["state"],))
            return _send(ctx, row, fn, arguments)
        if state in ("awaiting_approval", "pending", "failed_before_send"):
            row = _gate(ctx, row, target_version)
            row = _claim(hub_dir, row, (state,))
            return _send(ctx, row, fn, arguments)
        raise WorkItemError("invalid_state", f"Operation is in an unknown state {state!r}.", status=500)


def _gate(ctx, row, target_version):
    """Before any dispatch, first or resent: an operation in approve mode needs
    a current approval of this exact digest, by someone who still has access,
    of a target that has not changed since."""
    if row["mode"] != "approve":
        return row
    return _check_approval(ctx, row, target_version)


def _check_approval(ctx, row, target_version):
    hub_dir = ctx.hub_dir
    approval = row.get("approval_json") or {}
    if not approval or approval.get("digest") != row["args_digest"]:
        version = str(target_version()) if target_version else None
        if version != row.get("target_version"):
            row = store.update_op(hub_dir, row["id"], expected_revision=row["revision"],
                                  target_version=version)
        _park(ctx, row, "approval",
              "This action waits for a person to approve the exact content before it is sent.")
    if not _approver_still_allowed(hub_dir, ctx.hub, approval.get("approver") or ""):
        row = store.update_op(hub_dir, row["id"], expected_revision=row["revision"], approval_json=None,
                              state="awaiting_approval")
        _park(ctx, row, "approval", "The person who approved this no longer has access. "
              "It needs a fresh approval.", reason="approver_revoked")
    if target_version is not None:
        current = str(target_version())
        if current != approval.get("target_version"):
            row = store.update_op(hub_dir, row["id"], expected_revision=row["revision"],
                                  approval_json=None, target_version=current, state="awaiting_approval")
            _park(ctx, row, "approval", "The target changed after it was approved. "
                  "It needs a fresh approval of the current version.", reason="target_changed")
    return row


def _claim(hub_dir, row, from_states):
    attempts = list(row.get("attempts_json") or [])
    attempts.append({"n": len(attempts) + 1, "token": uuid.uuid4().hex, "state": "sending",
                     "started": time.time()})
    return store.update_op(hub_dir, row["id"], expected_revision=row["revision"],
                           expect_state=from_states, state="sending", attempts_json=attempts,
                           error=None)


def _recover(ctx, row, reconcile):
    """An earlier attempt was interrupted while sending, or failed with an
    unknown outcome. Decide by class. Returns (resend, row): resend is True
    when the same request may be sent again, else the row is settled or
    ambiguous."""
    hub_dir = ctx.hub_dir
    recovery = row["recovery"]
    if recovery == "idempotent":
        return True, row
    if recovery == "provider_key":
        if row.get("provider_key_expires") and time.time() < row["provider_key_expires"]:
            return True, row
        return False, store.update_op(hub_dir, row["id"], expected_revision=row["revision"], state="ambiguous",
                                      error="Interrupted after the provider's idempotency window ended.")
    if recovery == "reconcile" and reconcile is not None:
        try:
            found = reconcile(row_arguments(ctx.hub_dir, row))
        except Exception as exc:  # noqa: BLE001 - a failed check proves nothing
            found = Reconciled(UNKNOWN, evidence={"error": redaction.scrub_error(exc)})
        if not isinstance(found, Reconciled):
            raise WorkItemError("invalid_definition", "reconcile must return a Reconciled value.", status=500)
        evidence = _jsonable(redaction.redact(found.evidence))
        if found.status == APPLIED:
            return False, store.update_op(hub_dir, row["id"], expected_revision=row["revision"],
                                          state="succeeded", result_json=_jsonable(found.result),
                                          evidence_json=evidence)
        if found.status == SAFE_TO_RETRY:
            return True, store.update_op(hub_dir, row["id"], expected_revision=row["revision"],
                                         evidence_json=evidence)
        return False, store.update_op(hub_dir, row["id"], expected_revision=row["revision"], state="ambiguous",
                                      evidence_json=evidence,
                                      error="The check could not confirm whether the earlier attempt applied.")
    return False, store.update_op(hub_dir, row["id"], expected_revision=row["revision"], state="ambiguous",
                                  error="Interrupted while sending. The outcome is unknown.")


def row_arguments(hub_dir, row) -> dict:
    """The recorded arguments of an operation: what a check or resend uses."""
    return payload(hub_dir, row).get("arguments") or {}


def _jsonable(value):
    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


def _finish_attempt(row, state, **extra):
    attempts = list(row.get("attempts_json") or [])
    if attempts:
        attempts[-1] = dict(attempts[-1], state=state, ended=time.time(), **extra)
    return attempts


def _send(ctx, row, fn, arguments):
    hub_dir = ctx.hub_dir
    # A process that lost the hub's engine stops before the network call.
    from ..workflows import context
    context._owned(ctx)
    token = (row.get("attempts_json") or [{}])[-1].get("token")
    try:
        if row["recovery"] == "provider_key":
            result = fn(arguments, provider_key=row["provider_key"])
        else:
            result = fn(arguments)
    except NotSent as exc:
        _settle(hub_dir, row, token, "failed_before_send", error=redaction.scrub_error(exc))
        raise
    except Rejected as exc:
        _settle(hub_dir, row, token, "rejected", error=redaction.scrub_error(exc))
        raise OperationFailed("operation_rejected", redaction.scrub_error(exc), row["id"]) from None
    except Exception as exc:  # noqa: BLE001 - the outcome is now unknown
        message = redaction.scrub_error(exc)
        if row["recovery"] in ("idempotent", "provider_key"):
            # The outcome is unknown but a resend may be safe. It stays
            # `sending`, so the next attempt goes through recovery: the class's
            # own check (a provider key's window) and the approval gate.
            _settle(hub_dir, row, token, "sending", error=message)
            raise
        row = _settle(hub_dir, row, token, "ambiguous", error=message)
        if row is not None and row["state"] == "ambiguous":
            _park(ctx, row, "ambiguous", _UNKNOWN)
        raise
    row = _settle(hub_dir, row, token, "succeeded", result=_jsonable(result))
    if row is None or row["state"] not in ("succeeded", "resolved_done"):
        # A person resolved it while this attempt was in flight: theirs stands.
        current = row or {}
        return current.get("result_json")
    return row["result_json"]


def _settle(hub_dir, row, token, state, *, result=None, error=None):
    """Record this attempt's outcome, unless a newer attempt or a person's
    resolution already replaced it (a late result never overwrites that)."""
    current = store.get_op(hub_dir, row["id"])
    attempts = current.get("attempts_json") or []
    if current["state"] != "sending" or not attempts or attempts[-1].get("token") != token:
        return current
    fields = {"state": state, "error": error,
              "attempts_json": _finish_attempt(current, "unknown" if state == "sending" else state)}
    if result is not None or state == "succeeded":
        fields["result_json"] = result
    try:
        return store.update_op(hub_dir, row["id"], expected_revision=current["revision"],
                               expect_state=("sending",), **fields)
    except WorkItemError:
        return store.get_op(hub_dir, row["id"])


def view(row: dict, *, full: bool) -> dict:
    """An operation as people and the API see it. Payloads are never returned.
    `full` adds the target, results, errors and evidence, for the person
    allowed to decide. Others see what it is and its state only."""
    out = {k: row.get(k) for k in (
        "id", "item_id", "generation", "stage", "name", "action", "mode", "recovery", "state",
        "args_digest", "revision", "created", "updated")}
    out["attempts"] = [{k: a.get(k) for k in ("n", "state", "started", "ended")}
                       for a in (row.get("attempts_json") or [])]
    approval = row.get("approval_json") or None
    out["approval"] = ({"approver": approval.get("approver"), "at": approval.get("at")}
                       if approval else None)
    if full:
        out["target"] = row.get("target_json")
        out["target_version"] = row.get("target_version")
        out["error"] = row.get("error")
        out["result"] = row.get("result_json")
        out["evidence"] = row.get("evidence_json")
        out["resolution"] = row.get("resolution_json")
    return out


def payload(hub_dir, row: dict) -> dict:
    """The exact intent, decrypted, for the person deciding on it."""
    from .. import secretbox

    return secretbox.decrypt_json(hub_dir, row["payload_enc"])
