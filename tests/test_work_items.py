"""Work items and safe external writes, without the workflow engine.

The engine-backed journeys (redrive, crash recovery, continuation after a
decision) are in test_work_items_engine.py. These cover the rules every surface
shares: configuration, admission, write modes, the operation protocol and who
may decide.
"""
from __future__ import annotations

import textwrap

import pytest

from hubzoid import db
from hubzoid.work_items import Reconciled, Route, config, operations, service, store
from hubzoid.work_items.errors import NotSent, Parked, Rejected, WorkItemError
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity

OWNER = "admin@localhost"

WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"answer": Route(stages=("plan",), actions=()),
                          "act": Route(stages=("plan", "send"), actions=("email.send",))})
    def desk():
        return hub.work_item.checkpoint("plan", lambda: {"ok": True})
''')


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_AUTH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HUBZOID_OPERATIONAL_DB", raising=False)
    root = tmp_path / "desk"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    return root


def write_config(hub_dir, text):
    (hub_dir / "work-items.yaml").write_text(textwrap.dedent(text))


def run_ctx(hub_dir, item=None, stage=None):
    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    work = {"item": item, "stage": stage} if item else {}
    return context.run_scope(hub="desk", workflow="desk", hub_dir=hub_dir,
                             engine=db.operational_engine(hub_dir), identity=ident,
                             run_id="run-1", work=work)


class Sender:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self.fail is not None:
            raise self.fail
        return {"message_id": f"m-{len(self.calls)}"}


# -- configuration --------------------------------------------------------------

def test_config_defaults_and_validation(hub_dir):
    assert config.load(hub_dir).present is False
    write_config(hub_dir, """
        version: 1
        write_modes:
          email.send: draft
          erp.submit: "off"
    """)
    cfg = config.load(hub_dir)
    assert cfg.write_modes == {"email.send": "draft", "erp.submit": "off"}
    write_config(hub_dir, """
        version: 1
        work_items:
          max_pending: 5
          surprise: true
    """)
    with pytest.raises(WorkItemError) as err:
        config.load(hub_dir)
    assert err.value.code == "invalid_definition" and err.value.fields["line"] == 5
    write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: automatic\n")
    with pytest.raises(WorkItemError, match="off, draft or approve"):
        config.load(hub_dir)


def test_overlapping_write_modes_use_strictest_policy(hub_dir):
    cfg = config.parse("version: 1\nwrite_modes:\n  email.send: approve\n  customer_reply.send: draft\n")
    assert config.mode_for(cfg, ["email.send", "customer_reply.send"]) == "draft"
    assert config.mode_for(cfg, ["email.send"]) == "approve"
    assert config.mode_for(cfg, ["erp.submit"]) == "off"       # not named: off
    cfg = config.parse("version: 1\nwrite_modes:\n  email.send: approve\n  customer_reply.send: approve\n")
    assert config.mode_for(cfg, ["email.send", "customer_reply.send"]) == "approve"


def test_channel_override_preserves_email_draft():
    cfg = config.parse(textwrap.dedent("""
        version: 1
        write_modes:
          customer_reply.send: draft
          email.send: draft
        write_mode_overrides:
          whatsapp-desk:
            customer_reply.send: "off"
    """))
    assert config.mode_for(cfg, ["customer_reply.send"], "whatsapp-desk") == "off"
    assert config.mode_for(cfg, ["customer_reply.send", "email.send"], "email-desk") == "draft"
    config.NO_DRAFT_CHANNELS.add("whatsapp-desk")
    try:
        with pytest.raises(WorkItemError, match="no real drafts"):
            config.parse("version: 1\nwrite_mode_overrides:\n  whatsapp-desk:\n    customer_reply.send: draft\n")
    finally:
        config.NO_DRAFT_CHANNELS.discard("whatsapp-desk")


# -- admission ------------------------------------------------------------------

def test_duplicate_start_returns_the_same_item(hub_dir):
    item, created = service.admit(hub_dir, workflow="desk", key="operator:demo-1",
                                  input={"message": "hi"}, requester=OWNER)
    again, created_again = service.admit(hub_dir, workflow="desk", key="operator:demo-1",
                                         input={"message": "hi"}, requester=OWNER)
    assert created and not created_again and again["id"] == item["id"]
    assert item["generation"] == 1 and item["route"] == "answer" and item["state"] == "accepted"
    with pytest.raises(WorkItemError) as err:
        service.admit(hub_dir, workflow="desk", key="operator:demo-1", input={"message": "changed"})
    assert err.value.code == "key_conflict" and err.value.fields["item_id"] == item["id"]
    with pytest.raises(WorkItemError, match="still active"):
        service.admit(hub_dir, workflow="desk", key="operator:demo-1", input={"x": 1},
                      generation_mode="new")
    store.update_item(hub_dir, item["id"], state="completed")
    second, created = service.admit(hub_dir, workflow="desk", key="operator:demo-1", input={"x": 1},
                                    generation_mode="new")
    assert created and second["generation"] == 2
    # A replay of the original delivery still finds the latest generation's
    # key conflict, never a silent new item.
    with pytest.raises(WorkItemError):
        service.admit(hub_dir, workflow="desk", key="operator:demo-1", input={"message": "hi"})


def test_admission_limits_and_routes(hub_dir):
    with pytest.raises(WorkItemError) as err:
        service.admit(hub_dir, workflow="desk", key="k", route="implement")
    assert err.value.code == "route_missing"
    write_config(hub_dir, "version: 1\nwork_items:\n  max_pending: 1\n")
    service.admit(hub_dir, workflow="desk", key="a")
    with pytest.raises(WorkItemError) as err:
        service.admit(hub_dir, workflow="desk", key="b")
    assert err.value.code == "backlog_full" and err.value.status == 429
    write_config(hub_dir, "version: 1\nwork_items:\n  admission_paused: true\n")
    with pytest.raises(WorkItemError) as err:
        service.admit(hub_dir, workflow="desk", key="c")
    assert err.value.code == "admission_paused"


def test_scope_missing_and_wrong_target_deny(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", route="answer", requester=OWNER)
    send = Sender()
    with run_ctx(hub_dir, item):
        from hubzoid.work_items.facade import WorkItem

        with pytest.raises(WorkItemError) as err:
            WorkItem().checkpoint("send", lambda: 1)
        assert err.value.code == "scope_exceeded"
        with pytest.raises(WorkItemError) as err:
            operations.run(name="reply", target={"to": "x"}, arguments={}, recovery="escalate",
                           actions="email.send", dispatch=send)
        assert err.value.code == "scope_exceeded"
    assert send.calls == []


def test_counter_reservation_replay(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", requester=OWNER)
    from hubzoid.work_items.facade import WorkItem

    with run_ctx(hub_dir, item):
        assert WorkItem().reserve("review", "sha-1", 2) == 1
        assert WorkItem().reserve("review", "sha-1", 2) == 1      # same key: counted once
        assert WorkItem().reserve("review", "sha-2", 2) == 2
        assert WorkItem().reserve("review", "sha-3", 2) is None   # limit reached


def test_follow_up_messages_are_read_once(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", requester=OWNER)
    service.message(hub_dir, item["id"], OWNER, "Use the new address", client_message_id="c1")
    service.message(hub_dir, item["id"], OWNER, "Use the new address", client_message_id="c1")
    from hubzoid.work_items.facade import WorkItem

    with run_ctx(hub_dir, store.get_item(hub_dir, item["id"])):
        first = WorkItem().messages()
        assert [m["text"] for m in first] == ["Use the new address"]
        assert WorkItem().messages() == []
    with pytest.raises(WorkItemError):
        service.message(hub_dir, item["id"], "someone@else.org", "hello")


# -- the operation protocol -------------------------------------------------------

def test_changed_arguments_conflict(hub_dir):
    send = Sender()
    with run_ctx(hub_dir):
        first = operations.run(name="post", target={"issue": 7}, arguments={"body": "a"},
                               recovery="escalate", dispatch=send)
        again = operations.run(name="post", target={"issue": 7}, arguments={"body": "a"},
                               recovery="escalate", dispatch=send)
        assert first == again == {"message_id": "m-1"} and len(send.calls) == 1
        with pytest.raises(WorkItemError) as err:
            operations.run(name="post", target={"issue": 7}, arguments={"body": "b"},
                           recovery="escalate", dispatch=send)
    assert err.value.code == "operation_argument_conflict" and len(send.calls) == 1


def test_write_modes_hide_and_gate_tools(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", route="act", requester=OWNER)
    send, draft = Sender(), Sender()
    with run_ctx(hub_dir, item):
        with pytest.raises(WorkItemError) as err:   # no file: every action is off
            operations.run(name="reply", target={"t": 1}, arguments={}, recovery="escalate",
                           actions="email.send", dispatch=send, draft=draft)
        assert err.value.code == "write_mode_off"
        write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: draft\n")
        operations.run(name="reply", target={"t": 1}, arguments={"b": 1}, recovery="escalate",
                       actions="email.send", dispatch=send, draft=draft)
        assert len(draft.calls) == 1 and send.calls == []
        with pytest.raises(WorkItemError) as err:
            operations.run(name="reply2", target={"t": 1}, arguments={}, recovery="escalate",
                           actions="email.send", dispatch=send)
        assert err.value.code == "draft_unsupported"
        write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: approve\n")
        with pytest.raises(Parked) as parked:
            operations.run(name="reply3", target={"t": 1}, arguments={"b": 2}, recovery="escalate",
                           actions="email.send", dispatch=send)
        assert parked.value.kind == "approval"
    assert send.calls == []


def test_digest_bound_human_send(hub_dir):
    write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: approve\n")
    item, _ = service.admit(hub_dir, workflow="desk", key="k", route="act", requester=OWNER)
    send = Sender()
    args = dict(name="reply", target={"thread": "t1"}, arguments={"body": "Hello"},
                recovery="escalate", actions="email.send", dispatch=send)
    with run_ctx(hub_dir, item):
        with pytest.raises(Parked) as parked:
            operations.run(**args)
    op = store.get_op(hub_dir, parked.value.operation_id)
    assert op["state"] == "awaiting_approval" and op["requester"] == OWNER
    # Someone else cannot approve, and a stale digest is refused.
    with pytest.raises(WorkItemError) as err:
        service.approve(hub_dir, op["id"], "manager@company.org", digest=op["args_digest"])
    assert err.value.code == "approver_not_allowed"
    with pytest.raises(WorkItemError) as err:
        service.approve(hub_dir, op["id"], OWNER, digest="sha256:" + "0" * 64)
    assert err.value.code == "approval_stale"
    store.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": OWNER})
    with run_ctx(hub_dir, item):
        assert operations.run(**args) == {"message_id": "m-1"}
        assert operations.run(**args) == {"message_id": "m-1"}     # never sent twice
        # Changed bytes under the same key are refused, not sent.
        with pytest.raises(WorkItemError):
            operations.run(**dict(args, arguments={"body": "Changed"}))
    assert len(send.calls) == 1


def test_approval_dispatch_recheck(hub_dir):
    write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: approve\n")
    item, _ = service.admit(hub_dir, workflow="desk", key="k", route="act", requester=OWNER)
    send = Sender()
    version = {"v": "1"}
    args = dict(name="submit", target={"doc": "SO-1"}, arguments={"total": 10}, recovery="escalate",
                actions="email.send", dispatch=send, target_version=lambda: version["v"])
    with run_ctx(hub_dir, item):
        with pytest.raises(Parked) as parked:
            operations.run(**args)
    op = store.get_op(hub_dir, parked.value.operation_id)
    assert op["target_version"] == "1"
    store.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": OWNER,
                                                        "target_version": "1"})
    version["v"] = "2"                           # the record changed after approval
    with run_ctx(hub_dir, item):
        with pytest.raises(Parked) as parked:
            operations.run(**args)
    assert parked.value.reason == "target_changed"
    op = store.get_op(hub_dir, op["id"])
    assert op["approval_json"] is None and op["target_version"] == "2"
    # An approver who lost access no longer counts.
    store.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"],
                                                        "approver": "gone@company.org",
                                                        "target_version": "2"})
    with run_ctx(hub_dir, item):
        with pytest.raises(Parked) as parked:
            operations.run(**args)
    assert parked.value.reason == "approver_revoked" and send.calls == []


def _interrupted(hub_dir, recovery, **extra):
    """An operation left `sending` by a process that died mid-call."""
    send = Sender()
    with run_ctx(hub_dir):
        operations.run(name="probe", target={"n": recovery}, arguments={}, recovery=recovery,
                       dispatch=send, **extra)
    row = store.list_ops(hub_dir, "desk")[0]
    store.update_op(hub_dir, row["id"], state="sending", result_json=None)
    return row


def test_crash_after_provider_acceptance(hub_dir):
    row = _interrupted(hub_dir, "escalate")
    send = Sender()
    with run_ctx(hub_dir):
        with pytest.raises(Parked) as parked:
            operations.run(name="probe", target={"n": "escalate"}, arguments={},
                           recovery="escalate", dispatch=send)
    assert parked.value.kind == "ambiguous" and send.calls == []
    assert store.get_op(hub_dir, row["id"])["state"] == "ambiguous"


def test_reconcile_classes(hub_dir):
    _interrupted(hub_dir, "reconcile", reconcile=lambda a: Reconciled("unknown"))
    send = Sender()
    with run_ctx(hub_dir):
        # A check that finds the earlier write settles it with no resend.
        result = operations.run(name="probe", target={"n": "reconcile"}, arguments={},
                                recovery="reconcile", dispatch=send,
                                reconcile=lambda a: Reconciled("applied", {"id": 9}, {"found": 9}))
    assert result == {"id": 9} and send.calls == []


def test_empty_lookup_is_not_retry_proof(hub_dir):
    row = _interrupted(hub_dir, "reconcile", reconcile=lambda a: Reconciled("unknown"))
    send = Sender()
    with run_ctx(hub_dir):
        with pytest.raises(Parked):
            operations.run(name="probe", target={"n": "reconcile"}, arguments={}, recovery="reconcile",
                           dispatch=send, reconcile=lambda a: Reconciled("unknown", evidence={"listing": []}))
    assert send.calls == [] and store.get_op(hub_dir, row["id"])["state"] == "ambiguous"


def test_provider_key_expired(hub_dir):
    row = _interrupted(hub_dir, "provider_key", provider_key_ttl=60)
    store.update_op(hub_dir, row["id"], provider_key_expires=1.0)
    send = Sender()
    with run_ctx(hub_dir):
        with pytest.raises(Parked):
            operations.run(name="probe", target={"n": "provider_key"}, arguments={},
                           recovery="provider_key", dispatch=send, provider_key_ttl=60)
    assert send.calls == []


def test_idempotent_resend_and_outcome_classes(hub_dir):
    _interrupted(hub_dir, "idempotent")
    send = Sender()
    with run_ctx(hub_dir):
        operations.run(name="probe", target={"n": "idempotent"}, arguments={}, recovery="idempotent",
                       dispatch=send)
        assert len(send.calls) == 1
        with pytest.raises(NotSent):
            operations.run(name="x", target={}, arguments={}, recovery="escalate",
                           dispatch=Sender(NotSent("no connection")))
        assert operations.run(name="x", target={}, arguments={}, recovery="escalate",
                              dispatch=send) == {"message_id": "m-2"}
        with pytest.raises(operations.OperationFailed) as failed:
            operations.run(name="y", target={}, arguments={}, recovery="escalate",
                           dispatch=Sender(Rejected("400 bad address token=abc123secret")))
    assert failed.value.code == "operation_rejected" and "abc123secret" not in failed.value.message


def test_resolution_race_and_late_result(hub_dir):
    send = Sender(TimeoutError("socket closed"))
    with run_ctx(hub_dir):
        with pytest.raises(Parked):
            operations.run(name="mail", target={"to": "c"}, arguments={}, recovery="escalate",
                           dispatch=send)
    op = store.list_ops(hub_dir, "desk")[0]
    assert op["state"] == "ambiguous"
    # Only the requester decides, with evidence or an acknowledged risk.
    with pytest.raises(WorkItemError):
        service.resolve(hub_dir, op["id"], "other@company.org", decision="done", evidence="x")
    with pytest.raises(WorkItemError, match="evidence"):
        service.resolve(hub_dir, op["id"], OWNER, decision="done")
    with pytest.raises(WorkItemError, match="duplicate"):
        service.resolve(hub_dir, op["id"], OWNER, decision="retry")
    done = service.resolve(hub_dir, op["id"], OWNER, decision="done", evidence={"message_id": "m-77"})
    assert done["operation"]["state"] == "resolved_done"
    with pytest.raises(WorkItemError):       # one resolution wins
        service.resolve(hub_dir, op["id"], OWNER, decision="abandon")
    # A late result from the old attempt cannot overwrite the decision.
    row = store.get_op(hub_dir, op["id"])
    settled = operations._settle(hub_dir, row, "stale-token", "succeeded", result={"late": True})
    assert settled["state"] == "resolved_done"
    with run_ctx(hub_dir):
        assert operations.run(name="mail", target={"to": "c"}, arguments={}, recovery="escalate",
                              dispatch=Sender())["resolved_by_person"] is True


def test_view_hides_payloads_from_managers(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="k", input={"secret": "x"}, requester=OWNER)
    shown = service.show(hub_dir, item["id"], OWNER)
    assert shown["input"] == {"secret": "x"}
    with pytest.raises(WorkItemError):
        service.show(hub_dir, item["id"], "stranger@company.org")
    assert service.list_items(hub_dir, "stranger@company.org")["items"] == []


def test_idempotency_keys(hub_dir):
    calls = []

    def produce():
        calls.append(1)
        return 202, {"n": len(calls)}

    assert store.idempotent(hub_dir, "a@x.org", "start", "k1", {"x": 1}, produce) == (202, {"n": 1})
    assert store.idempotent(hub_dir, "a@x.org", "start", "k1", {"x": 1}, produce) == (202, {"n": 1})
    with pytest.raises(WorkItemError) as err:
        store.idempotent(hub_dir, "a@x.org", "start", "k1", {"x": 2}, produce)
    assert err.value.code == "idempotency_key_reused" and len(calls) == 1


def test_routes_are_validated():
    from hubzoid.work_items import normalize_routes

    assert normalize_routes(True) == {"default": {"stages": None, "actions": None}}
    assert normalize_routes({"plan": Route(stages=("plan",))})["plan"]["stages"] == ["plan"]
    with pytest.raises(ValueError):
        normalize_routes({"Bad Name": Route()})


def test_action_tool_follows_the_write_mode(hub_dir):
    import asyncio
    import json as _json

    from hubzoid import tool_classes
    from hubzoid.tools.actions import action_tool

    schema = {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}
    saved = []
    make = lambda: action_tool(hub_dir, name="draft_reply", description="Draft a reply.",  # noqa: E731
                               params_json_schema=schema, actions="email.send",
                               draft=lambda a: saved.append(a) or {"draft_id": "d1"})
    assert make() is None                                   # off: no tool
    write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: approve\n")
    assert make() is None                                   # approve: never in a model turn
    write_config(hub_dir, "version: 1\nwrite_modes:\n  email.send: draft\n")
    tool = make()
    assert tool is not None and tool_classes.classify(tool) == "external_write"
    out = _json.loads(asyncio.run(tool.on_invoke_tool(None, '{"body": "Hi"}')))
    assert out["saved"] and out["kind"] == "draft" and saved == [{"body": "Hi"}]


def test_redaction_and_classes():
    from hubzoid import redaction, tool_classes

    text = ("push failed: https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r "
            "Authorization: Bearer abcdefghijklmnop api_key=sk-ant-0123456789abcdefghijklmn")
    clean = redaction.scrub_text(text)
    assert "ghs_" not in clean and "abcdefghijklmnop" not in clean and "sk-ant" not in clean
    assert redaction.redact({"input_tokens": 12, "token": "x"}) == {"input_tokens": 12, "token": "[redacted]"}
    assert redaction.scrub_text("value 1234567890", secrets=["1234567890"]) == "value [redacted]"
    assert tool_classes.classify("read_file") == "read"
    assert tool_classes.classify("some_mcp_tool") == "external_write"
    with pytest.raises(ValueError):
        tool_classes.check("admin")
