"""Run lifecycle: a reply outlives its HTTP response, a stop keeps what was
written, and a bridge start marks replies left running by a previous process.

These tests drive server.build_app() on one event loop (httpx over ASGI, and a
raw ASGI call for a client that disconnects mid-stream), as uvicorn would.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.chat_helpers import ORIGIN, build_app, chat_body, events, make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _post_then_disconnect(app, body: dict, *, after_chunks: int) -> list[dict]:
    """POST /api/chat as a browser that closes the tab after a few chunks."""
    payload = json.dumps(body).encode()
    sent: list[dict] = []
    enough = asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await enough.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        bodies = [m for m in sent if m["type"] == "http.response.body" and m.get("body")]
        if len(bodies) >= after_chunks:
            enough.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/chat", "raw_path": b"/api/chat",
        "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_closing_the_browser_does_not_stop_the_reply(hub):
    app = build_app()
    runs = app.state.chat.runs
    agent = app.state.chat.model_label
    body = chat_body("c_away00001", "be slow", agent=agent, message_id="m_user00001",
                     assistant_id="m_asst00001")
    sent = await _post_then_disconnect(app, body, after_chunks=4)
    received = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"[DONE]" not in received            # the client left before the end
    run = runs._runs.get("m_asst00001")
    assert run is not None and not run.finished
    assert not run.subscribers                  # nobody is listening any more
    await asyncio.wait_for(run.done.wait(), timeout=30)
    reply = app.state.chat.store.get_message("m_asst00001")
    assert reply["status"] == "complete"
    text_ = reply["content"][0]["text"]
    assert text_.startswith("Chunk 1. ") and text_.endswith("Chunk 40. ")


@pytest.mark.asyncio
async def test_progress_is_saved_while_the_reply_runs(hub, monkeypatch):
    from hubzoid.chat import runs as runs_mod

    monkeypatch.setattr(runs_mod, "PERSIST_INTERVAL", 0.05)
    app = build_app()
    agent = app.state.chat.model_label
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        task = asyncio.create_task(client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_prog00001", "be slow", agent=agent, message_id="m_user00001",
            assistant_id="m_asst00001")))
        store = app.state.chat.store
        await _until(lambda: (m := store.get_message("m_asst00001")) is not None
                     and m["status"] == "running" and m["content"])
        partial = store.get_message("m_asst00001")["content"][0]["text"]
        assert partial.startswith("Chunk 1.") and not partial.endswith("Chunk 40. ")
        status = (await client.get("/api/runs/m_asst00001")).json()
        assert status["status"] == "running"
        assert status["message"]["content"][0]["text"].startswith("Chunk 1.")
        conv = (await client.get("/api/conversations/c_prog00001")).json()
        assert conv["messages"][1]["status"] == "running"
        await task
    assert store.get_message("m_asst00001")["status"] == "complete"


@pytest.mark.asyncio
async def test_stop_keeps_the_partial_reply(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "30")
    app = build_app()
    agent = app.state.chat.model_label
    runs = app.state.chat.runs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        task = asyncio.create_task(client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_stop00001", "be slow", agent=agent, message_id="m_user00001",
            assistant_id="m_asst00001")))
        await _until(lambda: (r := runs.active("m_asst00001")) is not None
                     and "Chunk 2." in r.builder.plain_text())
        r = await client.post("/api/runs/m_asst00001/cancel", headers=ORIGIN)
        assert r.status_code == 202 and r.json() == {"status": "cancelling"}
        response = await asyncio.wait_for(task, timeout=10)
        evs = events(response.text)
        assert evs[-2] == {"type": "finish", "messageMetadata": {"status": "cancelled"}}
        assert evs[-1] == "[DONE]"
        status = (await client.get("/api/runs/m_asst00001")).json()
        assert status["status"] == "cancelled"
        text_ = status["message"]["content"][0]["text"]
        assert text_.startswith("Chunk 1. Chunk 2.") and "Chunk 40." not in text_
        # stopping again is harmless
        again = await client.post("/api/runs/m_asst00001/cancel", headers=ORIGIN)
        assert again.status_code == 202 and again.json() == {"status": "cancelled"}
        # the conversation takes a new message right away
        after = await client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_stop00001", "thanks", agent=agent, message_id="m_user00002",
            assistant_id="m_asst00002", parent_id="m_asst00001"))
        assert after.status_code == 200
        assert '"messageMetadata":{"status":"complete"}' in after.text
    assert app.state.chat.inflight.busy() is False


@pytest.mark.asyncio
async def test_stopping_mid_tool_marks_the_tool_stopped(hub, monkeypatch):
    app = build_app()
    runtime = app.state.chat.runtime
    agent = app.state.chat.model_label
    from hubzoid.run_events import ToolCall

    gate = asyncio.Event()

    async def hanging_tool(prompt):  # noqa: ARG001
        yield "Looking it up."
        yield ToolCall(id="call_slow", name="slow_lookup", args={"q": "x"})
        await gate.wait()

    monkeypatch.setattr(runtime, "stream_events", hanging_tool)
    runs = app.state.chat.runs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        task = asyncio.create_task(client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_tool00001", "go", agent=agent, message_id="m_user00001", assistant_id="m_asst00001")))
        # The tool event is saved when it happens: the reply is stuck inside the
        # tool, so a save waiting for the next item would never come.
        store = app.state.chat.store

        def saved_tool():
            content = (store.get_message("m_asst00001") or {}).get("content") or []
            return content and content[-1].get("type") == "tool-call"

        await _until(saved_tool)
        stored = store.get_message("m_asst00001")["content"]
        assert stored[-1]["toolName"] == "slow_lookup" and "result" not in stored[-1]
        assert runs.active("m_asst00001") is not None
        await client.post("/api/runs/m_asst00001/cancel", headers=ORIGIN)
        evs = events((await task).text)
    assert {"type": "tool-output-error", "toolCallId": "call_slow",
            "errorText": "Stopped before this step finished."} in evs
    content = app.state.chat.store.get_message("m_asst00001")["content"]
    assert content == [{"type": "text", "text": "Looking it up."},
                       {"type": "tool-call", "toolCallId": "call_slow", "toolName": "slow_lookup",
                        "args": {"q": "x"},
                        "result": {"status": "error", "message": "Stopped before this step finished."}}]


@pytest.mark.asyncio
async def test_a_runtime_exception_fails_the_reply_not_the_bridge(hub, monkeypatch):
    app = build_app()
    runtime = app.state.chat.runtime
    agent = app.state.chat.model_label

    async def exploding(prompt):  # noqa: ARG001
        yield "Half an answer"
        raise ValueError("exploded")

    monkeypatch.setattr(runtime, "stream_events", exploding)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_boom00001", "go", agent=agent, message_id="m_user00001", assistant_id="m_asst00001"))
        evs = events(r.text)
        assert {"type": "error", "errorText": "ValueError: exploded"} in evs
        assert evs[-2]["messageMetadata"]["status"] == "error"
        reply = (await client.get("/api/runs/m_asst00001")).json()["message"]
        assert reply["status"] == "error" and reply["error"] == "ValueError: exploded"
        assert reply["content"] == [{"type": "text", "text": "Half an answer"}]
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_identity_and_chat_scope_inside_the_run(hub, monkeypatch):
    app = build_app()
    runtime = app.state.chat.runtime
    agent = app.state.chat.model_label
    seen = {}

    async def probe(prompt):  # noqa: ARG001
        from hubzoid import _request_ctx
        from hubzoid.access import current_identity

        ident = current_identity()
        seen.update(user=ident.user, surface=ident.surface, chat=_request_ctx.get_chat_id())
        yield "ok"

    monkeypatch.setattr(runtime, "stream_events", probe)
    busy_during = []
    original_enter = app.state.chat.inflight.enter

    def enter():
        original_enter()
        busy_during.append(app.state.chat.inflight.busy())

    monkeypatch.setattr(app.state.chat.inflight, "enter", enter)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_scope0001", "who am i", agent=agent, message_id="m_user00001",
            assistant_id="m_asst00001"))
    assert seen == {"user": "admin@localhost", "surface": "web", "chat": "c_scope0001"}
    assert busy_during == [True] and not app.state.chat.inflight.busy()


def test_bridge_start_marks_interrupted_replies(hub):
    app = build_app()
    store = app.state.chat.store
    store.create_conversation(conv_id="c_crash0001", owner_id="local-owner",
                              owner_email="admin@localhost", hub="chat-hub", agent="a")
    store.create_conversation(conv_id="c_crash0002", owner_id="local-owner",
                              owner_email="admin@localhost", hub="another-hub", agent="b")
    for conv, mid in (("c_crash0001", "m_crash0001"), ("c_crash0002", "m_crash0002")):
        store.insert_message(message_id=mid, conversation_id=conv, parent_id=None,
                             role="assistant", content=[{"type": "text", "text": "half"}],
                             text="half", status="running")
    build_app()   # the bridge restarts
    ours = store.get_message("m_crash0001")
    assert ours["status"] == "error" and ours["error"] == "Interrupted when the server restarted."
    assert ours["content"] == [{"type": "text", "text": "half"}]
    # another hub's bridge owns its own replies
    assert store.get_message("m_crash0002")["status"] == "running"


@pytest.mark.asyncio
async def test_shutdown_stops_and_saves_replies(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "30")
    app = build_app()
    agent = app.state.chat.model_label
    runs = app.state.chat.runs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        task = asyncio.create_task(client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_down00001", "be slow", agent=agent, message_id="m_user00001",
            assistant_id="m_asst00001")))
        await _until(lambda: (r := runs.active("m_asst00001")) is not None
                     and r.builder.plain_text())
        await runs.shutdown(timeout=5)
        await asyncio.wait_for(task, timeout=5)
    assert app.state.chat.store.get_message("m_asst00001")["status"] == "cancelled"
