"""Router transport isolation, refusal recovery, attachments and terminal work delivery."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from hubzoid.channels import admission, replies, router, store
from hubzoid.channels.errors import ChannelError
from tests.test_channels_contract import applied, ev, fake as fake, hub_dir as hub_dir, send


@pytest.fixture
def isolated_loop(monkeypatch):
    state = router._Loop()
    monkeypatch.setattr(router, "_state", state)
    return state


@pytest.mark.asyncio
async def test_transport_reload_replaces_revisions_and_isolates_failures(hub_dir, isolated_loop, monkeypatch):
    state = isolated_loop
    state.hub_dir = hub_dir
    old, current, made = Mock(), Mock(), Mock()
    old.stop.side_effect = RuntimeError("stop failed")
    state.transports = {"old@1": old, "keep@1": current}
    new_adapter = SimpleNamespace(start=Mock(return_value=made))
    bad_adapter = SimpleNamespace(start=Mock(side_effect=RuntimeError("start failed")))
    conns = [SimpleNamespace(id="keep", revision=1, enabled=True),
             SimpleNamespace(id="new", revision=2, enabled=True, adapter=new_adapter),
             SimpleNamespace(id="bad", revision=1, enabled=True, adapter=bad_adapter),
             SimpleNamespace(id="off", revision=1, enabled=False)]
    monkeypatch.setattr(router.config_lib, "active_all", lambda _: conns)
    await router.reload_transports()
    assert state.transports == {"keep@1": current, "new@2": made}
    assert old.stop.call_count == 1 and new_adapter.start.call_args.args == (conns[1], hub_dir)
    monkeypatch.setattr(router.config_lib, "active_all", Mock(side_effect=OSError("unreadable")))
    await router.reload_transports()
    assert state.transports == {"keep@1": current, "new@2": made}
    state.hub_dir = None
    await router.reload_transports()
    assert state.transports["new@2"] is made


@pytest.mark.asyncio
async def test_start_hooks_terminal_callback_and_stop_cancels_without_leaking(isolated_loop, tmp_path, monkeypatch):
    state = isolated_loop
    previous = Mock(side_effect=RuntimeError("other callback failed"))
    app = SimpleNamespace(state=SimpleNamespace(chat=SimpleNamespace(engine=SimpleNamespace(on_terminal=previous))))
    async def reload():
        return None
    async def run():
        await asyncio.Future()
    monkeypatch.setattr(router, "reload_transports", reload)
    monkeypatch.setattr(router, "_run", run)
    await router.start(app, tmp_path)
    assert state.task is not None and state.event.is_set()
    app.state.chat.engine.on_terminal(run="r1")
    previous.assert_called_once_with(run="r1")
    bad = Mock()
    bad.stop.side_effect = RuntimeError("stop failed")
    state.transports = {"bad": bad}
    await router.stop()
    assert state.task is None and state.transports == {} and state.loop is None and state.event is None
    assert bad.stop.call_count == 1


def test_kick_closed_loop_is_safe_and_notices_do_not_block_work(isolated_loop, hub_dir, fake, monkeypatch):
    isolated_loop.loop = Mock()
    isolated_loop.event = Mock()
    isolated_loop.loop.call_soon_threadsafe.side_effect = RuntimeError("closed")
    router.kick()
    assert isolated_loop.loop.call_soon_threadsafe.call_args.args == (isolated_loop.event.set,)
    conn = applied(hub_dir)
    row = send(conn, ev()).events[0]
    thread = store.get_thread(hub_dir, row["thread_id"])
    event = admission.event_of(hub_dir, row)
    post = Mock(side_effect=OSError("provider down"))
    monkeypatch.setattr(replies, "post", post)
    router._notice(conn, thread, event, "empty_request")
    assert post.call_args.kwargs["key"] == "notice:empty_request:U1"
    monkeypatch.setattr(fake, "acknowledge", Mock(side_effect=OSError("provider down")), raising=False)
    router._acknowledge(conn, thread, event, row)
    assert fake.acknowledge.call_count == 1


@pytest.mark.parametrize("error,status,outcome,reason", [
    (ChannelError("provider_unavailable", "retry", status=503), "accepted", "waiting", None),
    (ChannelError("provider_rejected", "no", status=422), "failed", "failed", "provider_rejected"),
    (ValueError("broken adapter"), "accepted", "error", None),
])
def test_provider_route_errors_restore_retryable_state(hub_dir, fake, monkeypatch, error, status, outcome, reason):
    conn = applied(hub_dir)
    row = send(conn, ev()).events[0]
    monkeypatch.setattr(fake, "route", Mock(side_effect=error))
    assert router._route_one(hub_dir, row, None) == (outcome, None)
    saved = store.get_delivery(hub_dir, row["id"])
    assert saved["status"] == status and saved["reason"] == reason


def test_adapter_authorization_denial_is_silent_and_claim_is_not_repeated(hub_dir, fake, monkeypatch):
    conn = applied(hub_dir)
    row = send(conn, ev()).events[0]
    monkeypatch.setattr(fake, "authorize", lambda *a: "not_collaborator")
    assert router._route_one(hub_dir, row, None) == ("denied", None)
    assert store.get_delivery(hub_dir, row["id"])["reason"] == "not_collaborator"
    assert fake.sent == []
    assert router._route_one(hub_dir, row, None) == ("skipped", None)


@pytest.mark.parametrize("status,expected", [(403, "hub_access_denied"), (503, "unavailable")])
def test_hub_permission_check_fails_closed(hub_dir, monkeypatch, status, expected):
    from hubzoid.chat import common
    monkeypatch.setattr(common, "check_use_hub", Mock(side_effect=HTTPException(status)))
    assert router._hub_access(hub_dir, "ana@example.org") == expected


def test_group_check_fails_closed_when_access_store_is_down(hub_dir, monkeypatch):
    from hubzoid import access
    conn = applied(hub_dir)
    monkeypatch.setattr(access, "store_for", Mock(side_effect=OSError("down")))
    assert router._groups_ok(conn, "ana@example.org") is False


@pytest.mark.parametrize("attachment_support", [True, False])
def test_content_keeps_text_and_explains_failed_attachments(hub_dir, fake, monkeypatch, attachment_support):
    conn = applied(hub_dir)
    row = send(conn, ev(text=" hello ", files=[{"name": "report.txt"}])).events[0]
    event = admission.event_of(hub_dir, row)
    person = SimpleNamespace(id="u1", email="ana@example.org")
    files = Mock()
    files.register_bytes.return_value = {"id": "file1"}
    files.attach.side_effect = RuntimeError("attach failed")
    chat = SimpleNamespace(files=files if attachment_support else None)
    monkeypatch.setattr(fake, "fetch_file", lambda *a: ("report.txt", "text/plain", b"report"), raising=False)
    parts = router._content(conn, event, person, chat, "c1", first=True)
    assert parts[0]["text"].startswith("(Fake thread t1)\n\nhello")
    if attachment_support:
        assert parts[1:] == [{"type": "file", "file_id": "file1"}]
        assert files.attach.call_args.args[1:] == ("file1", "c1")
        files.register_bytes.assert_called_once_with("u1", "report.txt", "text/plain", b"report", origin="channel")
    else:
        assert "report.txt was not added: this channel does not bring files in" in parts[0]["text"]
        assert len(parts) == 1


@pytest.mark.parametrize("results,expected", [([], "posted"), ([{"state": "succeeded"}], "posted"),
    ([{"state": "ambiguous"}], "uncertain"), ([{"state": "waiting"}], None),
    ([{"state": "failed", "code": "not_sent"}], None),
    ([{"state": "failed", "code": "operation_failed_before_send"}], None),
    ([{"state": "failed", "code": "rejected"}], "failed")])
def test_reply_settlement_outcomes(results, expected):
    assert router._outcome(results) == expected


@pytest.mark.parametrize("state,result,expected", [
    ("completed", {"posted": True}, None), ("completed", " hi ", "hi"),
    *[("completed", {key: " hello "}, "hello") for key in ("reply", "summary", "message", "text")],
    ("completed", {}, "Done (work item1234)."),
    ("cancelled", None, "Cancelled (work item1234). Anything already done is listed in the web app."),
    ("stopped", None, "Stopped (work item1234). It needs a person to decide in the web app before it can continue."),
    ("failed", None, "This work could not finish (work item1234). An administrator can see why in the web app."),
])
def test_terminal_text_does_not_expose_internal_errors(state, result, expected):
    assert router._final_text(state, result, "secret traceback", "item123456789") == expected


@pytest.mark.parametrize("item,customer,result_state,expected,replied", [
    (None, False, "succeeded", True, "no_item"),
    ({"state": "running"}, False, "succeeded", False, None),
    ({"state": "completed"}, True, "succeeded", True, "workflow_only"),
    ({"state": "completed", "result_json": {"result": {"posted": True}}}, False, "succeeded", True, "posted_by_workflow"),
    ({"state": "completed", "result_json": {"result": "All done"}}, False, "succeeded", True, "posted"),
    ({"state": "failed"}, False, "ambiguous", True, "uncertain"),
    ({"state": "failed"}, False, "waiting", False, None),
])
def test_work_item_final_reply_settles_only_known_outcomes(hub_dir, fake, monkeypatch, item, customer, result_state, expected, replied):
    from hubzoid.work_items import store as wstore
    conn = applied(hub_dir)
    row = send(conn, ev()).events[0]
    thread = store.get_thread(hub_dir, row["thread_id"])
    row = store.update_delivery(hub_dir, row["id"], route_json={"work_item_id": "i1", "account": "ana@example.org"})
    monkeypatch.setattr(wstore, "get_item", lambda *a: {"id": "i1", **item} if item else None)
    monkeypatch.setattr(fake, "customer_facing", customer)
    post = Mock(return_value=[{"state": result_state}])
    monkeypatch.setattr(replies, "post", post)
    assert router._work_item_reply(hub_dir, conn, thread, row) is expected
    assert store.get_delivery(hub_dir, row["id"])["route_json"].get("replied") == replied
    should_post = item is not None and item["state"] in ("completed", "failed") and not customer
    if item and isinstance((item.get("result_json") or {}).get("result"), dict):
        should_post = should_post and not item["result_json"]["result"].get("posted")
    assert post.call_count == int(should_post)



@pytest.mark.parametrize("value,expected", [(100, 100.0), (None, 0.0), ("bad", 0.0),
                                            ("1970-01-01T00:00:01Z", 1.0)])
def test_question_timestamp_parsing(value, expected):
    assert router._epoch(value) == expected


@pytest.mark.asyncio
async def test_route_pending_preserves_thread_order_and_launches_only_plans(hub_dir, monkeypatch):
    from hubzoid import drain
    from unittest.mock import AsyncMock
    monkeypatch.setattr(drain, "active", lambda: False)
    rows = [{"id": "a", "thread_id": "t1"}, {"id": "b", "thread_id": "t1"}, {"id": "c", "thread_id": "t2"}]
    monkeypatch.setattr(store, "open_deliveries", lambda *a: rows)
    route = Mock(side_effect=[("waiting", None), ("routed", "plan-c")])
    monkeypatch.setattr(router, "_route_one", route)
    launch = AsyncMock()
    app = SimpleNamespace(state=SimpleNamespace(chat=SimpleNamespace(orchestrator=SimpleNamespace(launch=launch))))
    assert await router.route_pending(hub_dir, app) == 1
    assert [c.args[1]["id"] for c in route.call_args_list] == ["a", "c"]
    launch.assert_awaited_once_with("plan-c")
    monkeypatch.setattr(drain, "active", lambda: True)
    assert await router.route_pending(hub_dir, app) == 0
