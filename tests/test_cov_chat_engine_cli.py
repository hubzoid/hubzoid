"""`hubzoid chat conversations|operations|branches|view|runs|queue|deletion`:
the local CLI over the chat operation engine, on a scratch hub with the
scripted test runtime. Each command's result, its refusals and exit codes.

Tests that need a finished reply (conversation lifecycle, branches, view,
queue, deletion preview) wait for #163: its CLI send fix, and its run status
fix, without which a fast reply can be swept as interrupted. `send` (a reply
through the web app) stays for them."""
from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hubzoid.cli import app
from tests.chat_helpers import ORIGIN, build_app, make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def cli(*args):
    """`hubzoid chat <args> --json`: the exit code and the parsed output."""
    r = CliRunner().invoke(app, ["chat", *args, "--json"], catch_exceptions=False)
    return r.exit_code, json.loads(r.output)


def created(hub):
    code, out = cli("conversations", "create", str(hub))
    assert code == 0, out
    return out["conversation_id"], out["branch"]


def send(client, cid, branch, text, parent=None):
    """One reply through the web app, waited for."""
    r = client.post(f"/api/conversations/{cid}/operations",
                    headers={**ORIGIN, "Idempotency-Key": str(uuid.uuid4())},
                    json={"kind": "send", "branch_id": branch["id"], "expected_revision": branch["revision"],
                          "parent_id": parent, "content": [{"type": "text", "text": text}]})
    assert r.status_code == 202, r.text
    out = r.json()
    end, run = time.time() + 20, {}
    while time.time() < end:
        run = client.get(f"/api/chat/runs/{out['run_id']}", headers=ORIGIN).json()
        if run["status"] in ("complete", "stopped", "interrupted", "failed"):
            break
        time.sleep(0.05)
    # The run's own state in the message, so a failure says why, not only that it waited.
    assert run.get("status") == "complete", f"the reply did not finish: {run}"
    return out


def test_a_temporary_conversation_is_kept_by_save(hub):
    code, out = cli("conversations", "create", str(hub), "--temporary")
    assert code == 0 and out["temporary"] is True and out["expires_at"]
    cid = out["conversation_id"]
    code, snap = cli("conversations", "get", cid, str(hub))
    rev = snap["conversation"]["graph_revision"]
    code, stale = cli("conversations", "save", cid, str(hub), "--expected-revision", str(rev + 1))
    assert code == 4 and stale["detail"]["code"] == "graph_conflict"
    code, saved = cli("conversations", "save", cid, str(hub), "--expected-revision", str(rev))
    assert code == 0 and saved == {"conversation_id": cid, "temporary": False}
    code, snap = cli("conversations", "get", cid, str(hub))
    assert snap["conversation"]["temporary"] is False and snap["conversation"]["expires_at"] is None


def test_refusals_name_the_problem(hub, tmp_path):
    cid, branch = created(hub)
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out = cli("conversations", "create", str(plain))
    assert code == 2 and out["detail"]["code"] == "invalid_request" and "not a hub folder" in out["detail"]["message"]

    code, out = cli("conversations", "get", cid, str(hub), "--user", "nobody@example.org")
    assert code != 0 and out["detail"]["code"] == "not_found" and "nobody@example.org" in out["detail"]["message"]

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    code, out = cli("operations", "submit", str(hub), "--conversation", cid, "--file", str(broken))
    assert code == 2 and out["detail"]["code"] == "invalid_request"
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2]")
    code, out = cli("operations", "submit", str(hub), "--conversation", cid, "--file", str(listed))
    assert code == 2 and "JSON object" in out["detail"]["message"]

    code, out = cli("runs", "get", "run-missing", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
    code, out = cli("operations", "get", "op-missing", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
