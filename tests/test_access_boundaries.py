"""Access boundaries across hubs, surfaces and identities (R1.10).

Each test names the boundary it holds: a grant in one hub never opens another,
identity cannot be forged past the bridge key or the public edge, a blocked
person is refused everywhere, a revoked grant stops a scheduled run's next call,
and every restricted call is in the decision log with its surface.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from agents.tool_context import ToolContext
from fastapi import HTTPException
from starlette.requests import Request

from hubzoid import access
from hubzoid.access import Identity, identity_scope
from hubzoid.access import audit as auditlib

_TOOL = '''
from agents import function_tool

@function_tool
def ledger_read(account: str = "all") -> str:
    "Read the ledger."
    return "ledger:" + account
'''


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL",
              "HUBZOID_DEPLOYMENT", "HUBZOID_RESTRICTED_SURFACES"):
        monkeypatch.delenv(k, raising=False)
    from hubzoid import migrations

    access._stores.clear()
    migrations._done.clear()
    auditlib._IMPORTED.clear()
    yield
    access._stores.clear()


def _hub(root: Path, name: str) -> Path:
    hub = root / name
    (hub / "restricted").mkdir(parents=True)
    (hub / "restricted" / "ledger.py").write_text(_TOOL)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
    return hub


@pytest.fixture
def gateway(tmp_path):
    """Two hubs on one shared operational store, both managed in the Console."""
    alpha, beta = _hub(tmp_path, "alpha"), _hub(tmp_path, "beta")
    from hubzoid import deployment

    deployment.save(tmp_path / "gw" / "deployment.json",
                    hubs=[dict(key=h.name, name=h.name, path=str(h), model_id=h.name) for h in (alpha, beta)],
                    operational_url=f"sqlite:///{tmp_path / 'gw' / 'ops.db'}",
                    owui_url="http://127.0.0.1:9", owui_db=str(tmp_path / "gw" / "webui.db"))
    gs = access.store_for(alpha)
    for h in ("alpha", "beta"):
        gs.set_authoritative(True, hub=h)
    return alpha, beta


def _tool(hub: Path):
    return access.apply(hub, {})["ledger_read"]


def _call(tool, ident: Identity) -> str:
    args = json.dumps({"account": "x"})
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    with identity_scope(ident):
        return asyncio.run(tool.on_invoke_tool(ctx, args))


def _request(**headers) -> Request:
    return Request({"type": "http", "headers": [
        (k.replace("_", "-").lower().encode(), v.encode()) for k, v in headers.items()]})


# ------------------------------------------------------------------ cross-hub


def test_a_grant_in_one_hub_never_opens_another(gateway):
    alpha, beta = gateway
    gs = access.store_for(alpha)
    gs.grant("alice@example.org", "alpha", "ledger", actor="test")
    alice = Identity.make("alice@example.org", surface="owui")

    assert _call(_tool(alpha), alice) == "ledger:x"
    assert "access denied" in _call(_tool(beta), alice)

    from hubzoid.server import _enforce_use_hub

    _enforce_use_hub(_request(x_openwebui_user_email="alice@example.org"), alpha)
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request(x_openwebui_user_email="alice@example.org"), beta)
    assert err.value.status_code == 403

    # Each hub's log holds only its own decisions.
    assert [r["decision"] for r in auditlib.read(alpha)] == ["allow"]
    assert [(r["decision"], r["reason"]) for r in auditlib.read(beta)] == [("deny", "no-grant")]


# ------------------------------------------------------------ forged identity


def test_body_user_is_not_an_identity_on_a_managed_hub(gateway):
    alpha, _ = gateway
    access.store_for(alpha).grant("alice@example.org", "alpha", "ledger", actor="test")
    from hubzoid.server import _derive_identity, _enforce_use_hub

    forged = _derive_identity({"user": "alice@example.org"}, _request(), alpha)
    assert forged.user is None
    assert "access denied" in _call(_tool(alpha), forged)
    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request(), alpha)
    assert err.value.status_code == 403 and "sign-in" in err.value.detail


def test_identity_headers_need_the_bridge_key(tmp_path, monkeypatch):
    hub = _hub(tmp_path, "solo")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "the-real-key")
    from fastapi.testclient import TestClient

    from hubzoid.server import build_app

    client = TestClient(build_app())
    body = {"model": "solo", "messages": [{"role": "user", "content": "hi"}]}
    for auth in ({}, {"Authorization": "Bearer guessed"}, {"Authorization": "Bearer dev"}):
        r = client.post("/v1/chat/completions", json=body,
                        headers={**auth, "X-OpenWebUI-User-Email": "admin@example.org",
                                 "X-Hubzoid-Groups": "ledger"})
        assert r.status_code == 401


# --------------------------------------------------------------- blocked user


def test_a_blocked_person_is_refused_everywhere(gateway):
    alpha, _ = gateway
    gs = access.store_for(alpha)
    gs.grant("bob@example.org", "alpha", "ledger", actor="test")
    gs.suspend("bob@example.org", actor="test")
    from hubzoid.server import _enforce_use_hub

    with pytest.raises(HTTPException) as err:
        _enforce_use_hub(_request(x_openwebui_user_email="bob@example.org"), alpha)
    assert err.value.status_code == 403 and "blocked" in err.value.detail
    for surface in ("owui", "mcp", "workflow"):
        assert "access denied" in _call(_tool(alpha), Identity.make("bob@example.org", surface=surface))
    assert {r["reason"] for r in auditlib.read(alpha)} == {"blocked"}

    # Blocking also removed the grants: reactivating does not bring them back.
    gs.suspend("bob@example.org", actor="test", suspended=False)
    assert "access denied" in _call(_tool(alpha), Identity.make("bob@example.org", surface="owui"))
    gs.grant("bob@example.org", "alpha", "ledger", actor="test")
    assert _call(_tool(alpha), Identity.make("bob@example.org", surface="owui")) == "ledger:x"


# ------------------------------------------------- revoked during a scheduled run


class _Runtime:
    """A stand-in agent: each round calls the restricted tool once."""

    def __init__(self, tool, gate):
        self.tool, self.gate, self.replies = tool, gate, []

    async def run(self, prompt: str) -> str:
        args = json.dumps({"account": "x"})
        ctx = ToolContext(context=None, tool_name=self.tool.name, tool_call_id="t", tool_arguments=args)
        self.replies.append(await self.tool.on_invoke_tool(ctx, args))
        self.gate(len(self.replies))
        return "STATUS: DONE" if len(self.replies) == 2 else "STATUS: CONTINUE round one"


def test_a_grant_revoked_mid_run_stops_the_next_call(gateway, tmp_path):
    alpha, _ = gateway
    (alpha / "schedule").mkdir()
    (alpha / "schedule" / "nightly.md").write_text(
        '---\nschedule: "0 3 * * *"\nmax_rounds: 2\n---\n\nRead the ledger.\n')
    from hubzoid import schedule_runner
    from hubzoid import scheduling as sch
    from hubzoid.access.store import GrantStore
    from hubzoid.db import operational_url
    from sqlalchemy import create_engine

    subject = schedule_runner.service_subject("nightly")
    assert subject == "workflow:md:nightly"
    access.store_for(alpha).grant(subject, "alpha", "ledger", actor="test")
    # The Console revokes from another process: a separate engine on the same DB.
    console = GrantStore(create_engine(operational_url(alpha)))

    def gate(n):
        if n == 1:
            console.revoke(subject, "alpha", "ledger", actor="admin@example.org")

    rt = _Runtime(_tool(alpha), gate)
    (task,), _ = sch.load_tasks(alpha)
    result = asyncio.run(schedule_runner.run_task(alpha, task, runtime_factory=lambda *a: rt,
                                                  capture=False))
    assert result.result == "done"
    assert rt.replies[0] == "ledger:x" and "access denied" in rt.replies[1]
    rows = auditlib.read(alpha)
    assert [(r["user"], r["surface"], r["decision"]) for r in rows] == [
        (subject, "workflow", "allow"), (subject, "workflow", "deny")]


# --------------------------------------------------------- audit completeness


@pytest.mark.parametrize("surface,expected", [
    ("owui", "allow"), ("mcp", "allow"), ("workflow", "allow"),
    ("slack", "deny"), ("slack-channel", "deny"), ("telegram", "deny"),
])
def test_every_surface_leaves_one_row(gateway, surface, expected):
    alpha, _ = gateway
    access.store_for(alpha).grant("carol@example.org", "alpha", "ledger", actor="test")
    _call(_tool(alpha), Identity.make("carol@example.org", surface=surface))
    (row,) = auditlib.read(alpha)
    assert (row["user"], row["surface"], row["tool"], row["decision"]) == (
        "carol@example.org", surface, "ledger_read", expected)


def test_a_call_that_cannot_be_logged_does_not_run(gateway, monkeypatch):
    alpha, _ = gateway
    access.store_for(alpha).grant("dan@example.org", "alpha", "ledger", actor="test")
    monkeypatch.setattr(auditlib, "record", lambda *a, **k: False)
    out = _call(_tool(alpha), Identity.make("dan@example.org", surface="owui"))
    assert "could not be recorded" in out and "ledger:" not in out


def test_old_decision_files_are_imported_once(tmp_path):
    hub = _hub(tmp_path, "old")
    (hub / "logs").mkdir()
    (hub / "logs" / "access-2026-08.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-08-01T10:00:00+00:00", "user": "Eve@Example.org", "surface": "owui",
         "tool": "ledger_read", "decision": "deny", "reason": "no-group"},
        {"ts": "2026-08-02T10:00:00+00:00", "user": "eve@example.org", "surface": "owui",
         "tool": "ledger_read", "decision": "allow", "reason": "group"},
        ["not", "a", "row"],
    ]) + "\nnot json\n")
    assert auditlib.import_legacy(hub) == 2
    auditlib._IMPORTED.clear()  # a second process
    assert auditlib.import_legacy(hub) == 0
    rows = auditlib.read(hub)
    assert [r["decision"] for r in rows] == ["deny", "allow"]
    assert (hub / "logs" / "access-2026-08.jsonl").exists()  # left in place
