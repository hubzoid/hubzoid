"""Decisions on work items and their operations (hubzoid.work_items.service):
only the person who asked may deny or resolve, each decision needs the right
state, done needs evidence and retry needs the duplicate risk accepted, a
denial stops the work, and resume or restart refuse what they can't do.
No workflow engine: items are admitted and operations recorded directly."""
from __future__ import annotations

import pytest

from hubzoid import db
from hubzoid.work_items import operations, service, store
from hubzoid.work_items.errors import WorkItemError
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity
from tests.test_cov_workflows_work_items_cli import OWNER, hub_dir  # noqa: F401


def recorded_op(hub_dir, state):
    item = service.admit(hub_dir, workflow="desk", key="ops-1", route="ops", requester=OWNER)[0]
    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    with context.run_scope(hub="desk", workflow="desk", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           identity=ident, run_id="run-1", work={"item": item, "stage": None}):
        operations.run(name="send", target={"to": "x"}, arguments={"body": "hi"}, recovery="idempotent",
                       actions="probe.call", dispatch=lambda args, **kw: {"ok": True})
    op = store.list_ops(hub_dir, "desk")[0]
    return item, store.update_op(hub_dir, op["id"], state=state)


def code(fn, *a, **kw):
    with pytest.raises(WorkItemError) as err:
        fn(*a, **kw)
    return err.value.code


def test_only_the_requester_denies_and_a_denial_stops_the_work(hub_dir):  # noqa: F811
    item, op = recorded_op(hub_dir, "awaiting_approval")
    assert code(service.deny, hub_dir, op["id"], "someone@else.org") == "approver_not_allowed"
    assert code(service.deny, hub_dir, "op-missing", OWNER) == "not_found"
    out = service.deny(hub_dir, op["id"], OWNER, reason="wrong customer")
    assert out["operation"]["state"] == "abandoned"
    assert store.get_item(hub_dir, item["id"])["state"] == "stopped"
    assert store.get_item(hub_dir, item["id"])["reason"] == "approval_denied"
    assert code(service.deny, hub_dir, op["id"], OWNER) == "not_waiting"
    assert code(service.approve, hub_dir, op["id"], OWNER, digest=op["args_digest"]) == "not_waiting"


def test_resolving_an_uncertain_operation(hub_dir):  # noqa: F811
    item, op = recorded_op(hub_dir, "ambiguous")
    assert code(service.resolve, hub_dir, op["id"], "someone@else.org", decision="done") == "resolver_not_allowed"
    assert code(service.resolve, hub_dir, op["id"], OWNER, decision="maybe") == "validation_failed"
    assert code(service.resolve, hub_dir, op["id"], OWNER, decision="done") == "validation_failed"
    assert code(service.resolve, hub_dir, op["id"], OWNER, decision="retry") == "validation_failed"
    out = service.resolve(hub_dir, op["id"], OWNER, decision="done", evidence="Sent, see message id 42")
    assert out["operation"]["state"] == "resolved_done"
    assert code(service.resolve, hub_dir, op["id"], OWNER, decision="done", evidence="x") == "not_ambiguous"


def test_abandoning_an_uncertain_operation_stops_the_work(hub_dir):  # noqa: F811
    item, op = recorded_op(hub_dir, "ambiguous")
    out = service.resolve(hub_dir, op["id"], OWNER, decision="abandon")
    assert out["operation"]["state"] == "abandoned"
    assert store.get_item(hub_dir, item["id"])["reason"] == "operation_abandoned"


def test_resume_and_restart_refusals(hub_dir):  # noqa: F811
    item, op = recorded_op(hub_dir, "ambiguous")
    with pytest.raises(WorkItemError) as err:
        service.resume(hub_dir, item["id"], "someone@else.org")
    assert err.value.status == 403
    assert code(service.resume, hub_dir, item["id"], OWNER) == "not_waiting"        # still accepted

    with pytest.raises(WorkItemError) as err:
        service.restart(hub_dir, item["id"], "someone@else.org", reason="x")
    assert err.value.status == 403
    assert code(service.restart, hub_dir, item["id"], OWNER, reason="again") == "not_restartable"
    store.update_item(hub_dir, item["id"], state="stopped")
    assert code(service.restart, hub_dir, item["id"], OWNER, reason="  ") == "validation_failed"
    assert code(service.restart, hub_dir, item["id"], OWNER, reason="again") == "operation_unresolved"
