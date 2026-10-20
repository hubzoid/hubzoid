"""Model choice, presets and side-by-side comparison (HUB-78, first part)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who
from tests.test_chat_operations import new_conv, op, snap, wait_run


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def catalog(hub):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    doc = svc.active().document
    doc["models"]["catalog"] = {"fast": {"label": "Fast", "model": "haiku"}, "deep": {"label": "Deep", "model": "opus"}}
    __import__('tests.chat_helpers', fromlist=['x']).write_chat_config(svc.hub_dir, doc)


@pytest.fixture
def claude(client, monkeypatch):
    """Comparison needs the runtime with a pre-tool hook (Claude); the scripted test runtime stands in."""
    monkeypatch.setattr(client.app.state.chat.models, "runtime_kind", lambda: "claude")


@pytest.fixture
def spy(client, monkeypatch):
    from hubzoid import _request_ctx

    seen = []
    runtime = client.app.state.chat.runtime
    original = runtime.stream_events

    def record(prompt):
        slot = _request_ctx.native_session()
        seen.append({"model": slot.model if slot else None, "tools": slot.tool_allowlist if slot else None})
        return original(prompt)

    monkeypatch.setattr(runtime, "stream_events", record)
    return seen


def test_catalog_and_per_turn_model(client, hub, spy, claude):
    # With no catalog configured, the Claude runtime offers its own aliases.
    assert [c["ref"] for c in client.get("/api/chat/models").json()["catalog"]] == ["default", "sonnet", "opus", "haiku"]
    catalog(hub)
    m = client.get("/api/chat/models").json()
    assert [c["ref"] for c in m["catalog"]] == ["default", "fast", "deep"] and m["can_choose"] and m["can_compare"]
    cid, b = new_conv(client)
    out = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"], "content": "hi",
                           "model_selection": {"mode": "model", "model": "haiku"}}).json()
    wait_run(client, out["run_id"])
    assert spy[-1]["model"] == "haiku"
    msg = next(x for x in snap(client, cid)["messages"] if x["id"] == out["assistant_message_id"])
    assert msg["requested_model"] == "haiku"
    b2 = client.get(f"/api/conversations/{cid}/branches", headers=ORIGIN).json()["items"][0]
    out2 = op(client, cid, {"kind": "send", "branch_id": b2["id"], "parent_id": out["assistant_message_id"],
                            "expected_revision": b2["revision"], "content": "again",
                            "model_selection": {"mode": "model", "model": "not-in-catalog"}}).json()
    wait_run(client, out2["run_id"])
    assert spy[-1]["model"] is None  # a model outside the catalog never reaches the runtime


def test_compare_refused_without_the_tool_hook(client, hub):
    catalog(hub)
    assert client.get("/api/chat/models").json()["can_compare"] is False  # scripted runtime: no hook
    cid, b = new_conv(client)
    first = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"], "content": "x"}).json()
    wait_run(client, first["run_id"])
    r = op(client, cid, {"kind": "compare", "target_message_id": first["user_message_id"], "model_refs": ["fast", "deep"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "compare_unsupported"


def test_claude_candidate_hook_matches_full_tool_identity():
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.factory_claude import _only_tools

    hook = _only_tools(ClaudeAgentOptions(), ["read_knowledge"]).hooks["PreToolUse"][0].hooks[0]
    ask = lambda name: asyncio.run(hook({"tool_name": name}, "t", None))
    assert ask("mcp__hubzoid__read_knowledge") == {}
    for name in ("mcp__untrusted__read_knowledge", "mcp__hubzoid__write_artifact", "WebSearch", "read_knowledge"):
        assert ask(name)["hookSpecificOutput"]["permissionDecision"] == "deny", name


def test_compare_answers_on_separate_read_only_branches(client, hub, spy, claude, monkeypatch):
    catalog(hub)
    cid, b = new_conv(client)
    first = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"],
                             "content": "compare me"}).json()
    wait_run(client, first["run_id"])
    out = op(client, cid, {"kind": "compare", "target_message_id": first["user_message_id"],
                           "model_refs": ["fast", "deep"]})
    assert out.status_code == 202, out.text
    cands = out.json()["candidates"]
    assert [c["model_ref"] for c in cands] == ["fast", "deep"] and len({c["branch_id"] for c in cands}) == 2
    for c in cands:
        wait_run(client, c["run_id"])
    runs = spy[-2:]
    assert sorted(r["model"] for r in runs) == ["haiku", "opus"]
    assert all(r["tools"] and "search_sources" in r["tools"] and "write_artifact" not in r["tools"] for r in runs)
    msgs = {m["id"]: m for m in snap(client, cid)["messages"]}
    assert {msgs[c["assistant_message_id"]]["parent_id"] for c in cands} == {first["user_message_id"]}
    bad = op(client, cid, {"kind": "compare", "target_message_id": first["user_message_id"], "model_refs": ["fast"]})
    assert bad.status_code == 422
    as_people(monkeypatch)
    cid2, b2 = new_conv(client, who("ana"))
    mine = op(client, cid2, {"kind": "send", "branch_id": b2["id"], "expected_revision": b2["revision"],
                             "content": "x"}, who("ana")).json()
    wait_run(client, mine["run_id"], who("ana"))
    denied = op(client, cid2, {"kind": "compare", "target_message_id": mine["user_message_id"],
                               "model_refs": ["fast", "deep"]}, who("ana"))
    assert denied.status_code == 403  # comparing needs the chat_model_compare grant


def test_presets_round_trip_and_shared_files(client, hub):
    catalog(hub)
    put = lambda pid, body: client.put(f"/api/chat/presets/{pid}", headers=ORIGIN, json=body)
    ok = put("quick", {"expected_revision": "absent", "name": "Quick answers", "model_ref": "fast", "default": True})
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    assert put("quick", {"expected_revision": "absent", "name": "x", "model_ref": "fast"}).status_code == 409
    assert put("bad", {"expected_revision": "absent", "name": "x", "model_ref": "gpt-9"}).status_code == 422
    assert put("both", {"expected_revision": "absent", "name": "x", "model_ref": "fast",
                        "candidate_refs": ["fast", "deep"]}).status_code == 422
    put("pair", {"expected_revision": "absent", "name": "Compare", "candidate_refs": ["fast", "deep"], "default": True})
    (hub / "presets").mkdir()
    (hub / "presets" / "team.yaml").write_text("version: 1\nid: team\nname: Team default\nmodel_ref: deep\n")
    items = {p["id"]: p for p in client.get("/api/chat/presets").json()["items"]}
    assert items["team"]["scope"] == "hub" and items["quick"]["default"] is False and items["pair"]["default"] is True
