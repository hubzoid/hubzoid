"""`hubzoid chat conversations|operations|branches|view|runs|queue|deletion`:
the local CLI over the chat operation engine, on a scratch hub with the
scripted test runtime. Each command's result, its refusals and exit codes.

In local mode the CLI and the web app both act as the local owner, so replies
are written through the HTTP API (the CLI's own send has a bug, tested in the
web app work's PR) and every other command runs through the CLI."""
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
    end = time.time() + 20
    while time.time() < end:
        if client.get(f"/api/chat/runs/{out['run_id']}", headers=ORIGIN).json()["status"] == "complete":
            return out
        time.sleep(0.05)
    raise AssertionError("the reply did not finish")


def test_conversation_lifecycle(hub, client):
    cid, branch = created(hub)
    assert branch["revision"] == 1 and branch["queue_paused"] is False
    out = send(client, cid, branch, "hello there")

    code, snap = cli("conversations", "get", cid, str(hub))
    assert code == 0 and snap["conversation"]["id"] == cid
    assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]

    code, receipt = cli("operations", "get", out["operation_id"], str(hub))
    assert code == 0 and receipt["operation_id"] == out["operation_id"]

    code, got = cli("runs", "get", out["run_id"], str(hub))
    assert code == 0 and got["status"] == "complete" and got["conversation_id"] == cid
    code, watched = cli("runs", "watch", out["run_id"], str(hub))
    assert code == 0 and watched["status"] == "complete"
    code, stop = cli("runs", "cancel", out["run_id"], str(hub))
    assert code == 0 and stop == {"run_id": out["run_id"], "status": "complete", "already_terminal": True}

    rev = snap["conversation"]["graph_revision"]
    code, gone = cli("conversations", "delete", cid, str(hub), "--expected-revision", str(rev + 5))
    assert code == 4 and gone["detail"]["code"] == "graph_conflict"
    code, gone = cli("conversations", "delete", cid, str(hub), "--expected-revision", str(rev))
    assert code == 0 and gone["chat_key"]
    assert not (hub / ".hubzoid" / "chats" / gone["chat_key"]).exists()
    code, missing = cli("conversations", "get", cid, str(hub))
    assert code != 0 and missing["detail"]["code"] == "not_found"


def test_branches_view_queue_controls_and_deletion_preview(hub, client):
    cid, branch = created(hub)
    first = send(client, cid, branch, "first")

    code, listed = cli("branches", "list", str(hub), "--conversation", cid)
    assert code == 0 and [b["id"] for b in listed["items"]] == [branch["id"]]
    code, forked = cli("branches", "create", str(hub), "--conversation", cid, "--from", first["user_message_id"])
    assert code == 0, forked
    fork_id = forked["branch"]["id"] if "branch" in forked else forked["id"]
    assert fork_id != branch["id"]
    code, listed = cli("branches", "list", str(hub), "--conversation", cid)
    assert sorted(b["id"] for b in listed["items"]) == sorted([branch["id"], fork_id])

    code, view = cli("view", "set", str(hub), "--conversation", cid, "--branch", fork_id)
    assert code == 0 and json.dumps(view).count(fork_id) >= 1

    code, queue = cli("queue", "list", str(hub), "--conversation", cid)
    assert code == 0 and queue["items"] == []
    code, paused = cli("queue", "pause", str(hub), "--conversation", cid, "--branch", branch["id"])
    assert code == 0 and paused["branch"]["queue_paused"] is True and "run" not in paused
    code, resumed = cli("queue", "resume", str(hub), "--conversation", cid, "--branch", branch["id"])
    assert code == 0 and resumed["branch"]["queue_paused"] is False
    code, cleared = cli("queue", "clear", str(hub), "--conversation", cid, "--branch", branch["id"])
    assert code == 0 and cleared["branch"]["id"] == branch["id"]

    code, preview = cli("deletion", "preview", str(hub), "--conversation", cid,
                        "--message", first["user_message_id"])
    assert code == 0 and first["user_message_id"] in json.dumps(preview)


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
