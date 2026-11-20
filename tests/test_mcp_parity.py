"""An assistant over hosted MCP does what chat does (proposals/2026-11-20-mcp-parity.md).

Files in each person's own space with a download link and a resource to read
back, an upload the hub's tools read, follow-up questions and approvals asked
through MCP elicitation (with a session) or handed back to the assistant
(without), remember and the hub change tools under chat's gate, core skills,
and sub-agents on their own model only with the ``mcp_delegates`` grant.
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket

import pytest

from tests import connect_helpers as h

X, Y = "x@example.org", "y@example.org"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from tests.mcp_credentials import RESOURCE, seed

    hub = tmp_path / "parity"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: parity\n---\nYou help.\n")
    (hub / "knowledge" / "intro.md").write_text("# Intro\nHello.\n")
    agent = hub / "agents" / "deep"
    agent.mkdir(parents=True)
    (agent / "AGENTS.md").write_text("---\nname: deep\ndescription: Thinks hard.\nmodel: claude-local/opus\n"
                                     "---\nThink hard and answer.\n")
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("MCP_PUBLIC_URL", RESOURCE)
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example")
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.delenv("HUBZOID_RESTRICTED_SURFACES", raising=False)
    seed(hub, token="mcp-x", email=X, account_id="ux")
    seed(hub, token="mcp-y", email=Y, account_id="uy")
    h.grant(hub, X, Y, permissions=("use_hub",))
    return hub


def _app(hub):
    from hubzoid import mcp_server

    return mcp_server.build_mcp_app(hub)


def _call(name, **arguments):
    return ("tools/call", {"name": name, "arguments": arguments})


async def _hosted(hub, *calls):
    return await h.hosted(_app(hub), *[(tok, m, p) for tok, (m, p) in calls])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_files_land_in_each_callers_own_space_with_a_link_and_a_resource(hub):
    x, y = await _hosted(hub, ("mcp-x", _call("write_artifact", filename="a.txt", content="from x")),
                         ("mcp-y", _call("list_artifacts")))
    content = x["result"]["content"]
    assert "/artifacts/mcp-" in content[0]["text"] and "?t=" in content[0]["text"]
    link = next(c for c in content if c["type"] == "resource_link")
    assert link["uri"] == "hubzoid-file://artifacts/a.txt"
    assert "a.txt" not in h.text_of(y)                          # Y's space is their own
    (mine,) = await _hosted(hub, ("mcp-x", ("resources/read", {"uri": link["uri"]})))
    assert mine["result"]["contents"][0]["text"] == "from x"
    (theirs,) = await _hosted(hub, ("mcp-y", ("resources/read", {"uri": link["uri"]})))
    assert "error" in theirs


@pytest.mark.asyncio
async def test_an_upload_is_read_by_the_hubs_upload_tools(hub):
    csv = base64.b64encode(b"name,qty\nbolt,4\n").decode()
    up, read = await _hosted(hub, ("mcp-x", _call("upload_file", filename="stock.csv", content=csv,
                                                  encoding="base64")),
                             ("mcp-x", _call("read_upload_full", filename="stock.csv")))
    assert "Saved stock.csv" in h.text_of(up)
    # The two calls ran at once; read again now the upload is surely there.
    (read,) = await _hosted(hub, ("mcp-x", _call("read_upload_full", filename="stock.csv")))
    assert "bolt,4" in h.text_of(read)
    (other,) = await _hosted(hub, ("mcp-y", _call("read_upload", filename="stock.csv")))
    assert "not found" in h.text_of(other)


@pytest.mark.asyncio
async def test_upload_refuses_too_large_and_bad_base64(hub, monkeypatch):
    from hubzoid import mcp_parity

    monkeypatch.setattr(mcp_parity, "MAX_INLINE_UPLOAD", 10)
    big, bad = await _hosted(hub, ("mcp-x", _call("upload_file", filename="b.txt", content="x" * 11)),
                             ("mcp-x", _call("upload_file", filename="c.bin", content="!!", encoding="base64")))
    assert "the limit over MCP" in h.text_of(big)
    assert "not valid base64" in h.text_of(bad)


# ---------------------------------------------------------------------------
# Skills, remember, delegates
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_core_skills_and_remember_follow_chats_rules(hub, monkeypatch):
    from hubzoid.access import store_for

    (listed,) = await _hosted(hub, ("mcp-x", ("tools/list", {})))
    names = h.tool_names(listed)
    assert "remember" not in names                              # needs Save shared knowledge
    (skills,) = await _hosted(hub, ("mcp-x", _call("list_skills")))
    assert "dashboard" in h.text_of(skills)                     # its write_artifact works here now
    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "true")
    store_for(hub).grant(X, hub.name, "curator", actor="test")
    (listed,) = await _hosted(hub, ("mcp-x", ("tools/list", {})))
    assert "remember" in h.tool_names(listed)
    (saved,) = await _hosted(hub, ("mcp-x", _call("remember", topic="Returns", text="Refunds take 5 days.")))
    assert "change #" in h.text_of(saved)                       # proposed or applied, as in chat


@pytest.mark.asyncio
async def test_a_delegate_needs_the_grant_and_runs_the_hubs_runtime_as_the_caller(hub, monkeypatch):
    from hubzoid import runtime, run_events, usage
    from hubzoid.access import current_identity, store_for

    (listed,) = await _hosted(hub, ("mcp-x", ("tools/list", {})))
    assert "handover_deep" not in h.tool_names(listed)
    (refused,) = await _hosted(hub, ("mcp-x", _call("handover_deep", task="why?")))
    assert h.failed(refused) or "access denied" in h.text_of(refused)

    seen, rows = [], []

    class Fake:
        last_error = None

        async def aopen(self): ...

        async def aclose(self): ...

    async def answer(rt, prompt):
        seen.append((prompt, current_identity().user, current_identity().surface))
        return "deep answer"

    monkeypatch.setattr(runtime, "build", lambda hub_dir, **kw: Fake())
    monkeypatch.setattr(run_events, "answer_only", answer)
    monkeypatch.setattr(usage, "record", lambda *a, **kw: rows.append(kw))
    store_for(hub).grant(X, hub.name, "mcp_delegates", actor="test")
    (listed,) = await _hosted(hub, ("mcp-x", ("tools/list", {})))
    assert "handover_deep" in h.tool_names(listed)
    (done,) = await _hosted(hub, ("mcp-x", _call("handover_deep", task="why?")))
    assert h.text_of(done) == "deep answer"
    prompt, who, surface = seen[0]
    assert "`deep` sub-agent" in prompt and "why?" in prompt and (who, surface) == (X, "mcp")
    assert rows[0]["surface"] == "mcp" and rows[0]["subject"] == X and rows[0]["kind"] == "delegate"


# ---------------------------------------------------------------------------
# Questions and approvals
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_without_elicitation_the_assistant_asks_and_approval_says_where(hub):
    (hub / "hitl.yaml").write_text("version: 1\ntools:\n  current_time: {ask: always}\n")
    q, t = await _hosted(hub, ("mcp-x", _call("ask_question", question="Which region?")),
                         ("mcp-x", _call("current_time")))
    assert json.loads(h.text_of(q))["status"] == "ask_in_conversation"
    waiting = json.loads(h.text_of(t))
    assert waiting["status"] == "waiting"
    assert "https://hub.example/requests" in waiting["message"] and "same arguments" in waiting["message"]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _session(hub, answers: list, *calls):
    """A real MCP client with a session and an elicitation handler, as Claude
    Code connects. ``answers`` are given in order; each request is recorded."""
    import uvicorn
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth
    from fastmcp.client.transports import StreamableHttpTransport

    app = _app(hub)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    asked: list[str] = []
    try:
        while not server.started:
            await asyncio.sleep(0.05)

        async def handler(message, response_type, params, context):
            asked.append(message)
            return {"value": answers.pop(0)}

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=BearerAuth("mcp-x"))
        out = []
        async with Client(transport, elicitation_handler=handler) as client:
            for name, args in calls:
                result = await client.call_tool(name, args, raise_on_error=False)
                out.append(result.content[0].text)
        return out, asked
    finally:
        server.should_exit = True
        await task


@pytest.mark.slow
@pytest.mark.asyncio
async def test_with_elicitation_the_person_answers_and_approves_in_place(hub, monkeypatch):
    monkeypatch.delenv("MCP_STATELESS", raising=False)
    (hub / "hitl.yaml").write_text("version: 1\ntools:\n  current_time: {ask: always}\n")
    out, asked = await _session(
        hub, ["EU", "Approve", "Reject"],
        ("ask_question", {"question": "Which region?", "options": [{"label": "EU"}, {"label": "US"}]}),
        ("current_time", {}), ("current_time", {}))
    assert json.loads(out[0]) == {"status": "answered", "answer": "EU"}
    assert "UTC" in out[1] or ":" in out[1]                     # approved, so it ran once
    assert json.loads(out[2])["status"] == "rejected"           # rejected, so it did not run
    assert asked[0].startswith("Which region?") and asked[1].startswith("Approve")


@pytest.mark.asyncio
async def test_a_workflow_runs_questions_are_answered_in_place_or_sent_to_the_inbox(hub, monkeypatch):
    """A run started over MCP that stops to ask: answered through elicitation,
    then its next request too; without elicitation, the inbox link."""
    from hubzoid import mcp_parity
    from hubzoid.access import Identity
    from hubzoid.hitl import service

    who = service.Caller(account_id="ux", email=X, origin="workflow", present=False)
    ask = service.create(hub, "parity", who, kind="question", title="Which warehouse?",
                         question="Which warehouse?", options=[{"label": "North"}, {"label": "South"}],
                         workflow_id="run-1")
    waiting = json.dumps({"status": "waiting", "request_id": ask["id"], "kind": "question"})
    ident = Identity.make(user=X, groups=[], surface="mcp", account_id="ux")

    out = json.loads(await mcp_parity.settle_waiting(hub, ident, waiting, None))
    assert out["status"] == "waiting" and "https://hub.example/requests" in out["message"]

    class Answer:
        def __init__(self, data):
            self.action, self.data = "accept", data

    answers = iter(["North", "Approve"])

    async def elicit(ctx, message, response_type):
        if "North" in str(response_type):
            service.create(hub, "parity", who, kind="approval", title="Reserve 20", workflow_id="run-1")
        return Answer(next(answers))

    monkeypatch.setattr(mcp_parity, "can_elicit", lambda ctx=None: True)
    monkeypatch.setattr(mcp_parity, "_elicit", elicit)
    monkeypatch.setattr(mcp_parity, "NEXT_REQUEST_SECONDS", 3.0)
    out = json.loads(await mcp_parity.settle_waiting(hub, ident, waiting, None))
    assert out["status"] == "decided"
    assert 'Answered "Which warehouse?": North.' in out["message"] and 'Approved "Reserve 20".' in out["message"]
    assert service.get(hub, "parity", ask["id"])["answer"] == "North"
