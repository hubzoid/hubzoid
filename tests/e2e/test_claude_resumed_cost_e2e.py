"""A real chat on claude-local: each turn's usage row holds its own cost.

The chat engine resumes the branch's Claude session on every turn, and Claude
Code reports a resumed session's running total. Three turns of one chat must
add up to the session's total that the binding keeps, not to more: before the
fix the rows held 1x, 2x and 3x the first turn's cost.

How to run.
    pytest tests/e2e/test_claude_resumed_cost_e2e.py -m e2e -v

Three short Haiku turns from the signed-in Claude subscription. Self-skips
without the `claude` CLI.
"""
from __future__ import annotations

import shutil
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, build_app, make_hub

pytestmark = pytest.mark.e2e


@pytest.fixture
def client(tmp_path, monkeypatch):
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH")
    hub = make_hub(tmp_path, monkeypatch, MODEL="claude-local/haiku")
    monkeypatch.delenv("HUBZOID_TEST_RUNTIME", raising=False)
    with TestClient(build_app()) as c:
        yield c, hub


def _send(c, cid, branch, text, parent):
    r = c.post(f"/api/conversations/{cid}/operations", headers={**ORIGIN, "Idempotency-Key": str(uuid.uuid4())},
               json={"kind": "send", "branch_id": branch["id"], "expected_revision": branch["revision"],
                     "parent_id": parent, "content": [{"type": "text", "text": text}]})
    assert r.status_code == 202, r.text
    run = r.json()
    end = time.time() + 180
    while time.time() < end:
        s = c.get(f"/api/chat/runs/{run['run_id']}", headers=ORIGIN).json()
        if s["status"] in ("complete", "stopped", "interrupted", "failed"):
            assert s["status"] == "complete", s
            return run
        time.sleep(0.5)
    raise AssertionError("the reply did not finish")


def _branch(c, cid, bid):
    snap = c.get(f"/api/conversations/{cid}", headers=ORIGIN).json()
    return next(b for b in snap["branches"] if b["id"] == bid)


def test_three_resumed_turns_add_up_to_the_session_total(client):
    from hubzoid import db
    from hubzoid.chat import graph

    c, hub = client
    r = c.post("/api/conversations", headers=ORIGIN, json={"agent": c.app.state.chat.model_label})
    assert r.status_code == 201, r.text
    cid, branch = r.json()["conversation"]["id"], r.json()["branch"]
    parent = None
    for text in ("Reply with only the word one.", "Reply with only the word two.",
                 "Reply with only the word three."):
        run = _send(c, cid, _branch(c, cid, branch["id"]), text, parent)
        parent = run["assistant_message_id"]

    engine = db.operational_engine(hub)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT cost_usd, input_tokens, chat_id FROM hz_usage "
                                    "WHERE kind='chat' ORDER BY ts")).fetchall()
        binding = conn.execute(sa.select(graph.bindings)).fetchall()
    print("usage rows:", [(round(r.cost_usd or 0, 6), r.input_tokens) for r in rows])
    print("binding:", [(b.native_session_id[:8], b.native_cost_usd) for b in binding])
    assert len(rows) == 3 and all((r.cost_usd or 0) > 0 for r in rows)
    assert len(binding) == 1 and binding[0].native_cost_usd
    session_total = binding[0].native_cost_usd
    # The three turns ran in one resumed session, and its total is counted once.
    assert sum(r.cost_usd for r in rows) == pytest.approx(session_total, rel=0.02)
    # a resumed turn costs less than the session so far (before the fix it held the total)
    assert rows[2].cost_usd < 0.9 * session_total
