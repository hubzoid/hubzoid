"""`hubzoid chat feedback|helpers|title|notifications`: ratings, titles, the
inbox and notification targets through the local CLI, with their refusals."""
from __future__ import annotations

import json

import pytest

from hubzoid.chat import feedback as fb
from tests.test_cov_chat_engine_cli import cli, client, created, hub, send  # noqa: F401


@pytest.fixture
def answered(hub, client):  # noqa: F811
    cid, branch = created(hub)
    out = send(client, cid, branch, "how many days of leave?")
    return cid, branch, out["assistant_message_id"]


def test_rating_put_list_export_and_remove(hub, answered, tmp_path):  # noqa: F811
    cid, _, msg = answered
    code, out = cli("feedback", "put", str(hub), "--conversation", cid, "--message", msg, "--rating", "sideways")
    assert code == 2 and "positive or negative" in out["detail"]["message"]
    code, out = cli("feedback", "put", str(hub), "--conversation", cid, "--message", msg,
                    "--rating", "negative", "--comment", "Missed the policy", "--tag", "policy")
    assert code == 0 and out["rating"] == "negative" and out["comment"] == "Missed the policy"
    rev = out["revision"]
    code, out = cli("feedback", "put", str(hub), "--conversation", cid, "--message", msg, "--rating", "positive")
    assert code == 4 and out["detail"]["code"]  # it exists now: the default 'absent' revision is stale

    code, listed = cli("feedback", "list", str(hub), "--rating", "negative", "--source", "cli")
    assert code == 0 and [i["message_id"] for i in listed["items"]] == [msg]
    code, listed = cli("feedback", "list", str(hub), "--rating", "positive")
    assert listed["items"] == []

    target = tmp_path / "ratings.csv"
    code, out = cli("feedback", "export", str(hub), "--format", "csv", "--output", str(target))
    assert code == 0 and out["bytes"] == target.stat().st_size and "Missed the policy" in target.read_text()
    code, out = cli("feedback", "export", str(hub), "--format", "xml", "--output", str(target))
    assert code == 2 and out["detail"]["message"] == "format is json or csv."

    code, out = cli("feedback", "remove", str(hub), "--conversation", cid, "--message", msg,
                    "--expected-revision", str(rev))
    assert code == 0 and out == {"deleted": True}
    code, listed = cli("feedback", "list", str(hub))
    assert listed["items"] == []


def test_title_set_and_its_limits(hub, answered):  # noqa: F811
    cid, _, _ = answered
    code, out = cli("title", "set", str(hub), "--conversation", cid, "--title", "   ")
    assert code == 2 and "1 to 200 characters" in out["detail"]["message"]
    code, out = cli("title", "set", str(hub), "--conversation", cid, "--title", "x" * 201)
    assert code == 2
    code, out = cli("title", "set", str(hub), "--conversation", cid, "--title", "  Leave   policy  ")
    assert code == 0 and out == {"id": cid, "title": "Leave policy", "source": "user"}
    code, snap = cli("conversations", "get", cid, str(hub))
    assert snap["conversation"]["title"] == "Leave policy" and snap["conversation"]["title_source"] == "user"


def test_helpers_list_and_inbox(hub, answered):  # noqa: F811
    cid, branch, _ = answered
    code, out = cli("helpers", "list", str(hub), "--conversation", cid, "--branch", branch["id"])
    assert code == 0 and isinstance(out["items"], list)
    code, inbox = cli("notifications", "list", str(hub))
    assert code == 0 and "items" in inbox
    code, out = cli("notifications", "read", "n-missing", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"


def test_notification_targets(hub, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HZ_HOOK", "s3cret")
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
    code, out = cli("notifications", "targets", "get", str(hub))
    assert code == 0 and out["items"] == []

    body = tmp_path / "target.json"
    body.write_text(json.dumps({"type": "pager", "recipient_ref": "x"}))
    code, out = cli("notifications", "targets", "put", "ops", str(hub), "--file", str(body))
    assert code == 2 and out["detail"]["message"] == "type is webhook, email or slack."
    body.write_text(json.dumps({"type": "webhook", "recipient_ref": "https://hooks.example.com/x"}))
    code, out = cli("notifications", "targets", "put", "ops", str(hub), "--file", str(body))
    assert code == 2 and "signing secret" in out["detail"]["message"]
    body.write_text(json.dumps({"type": "webhook", "recipient_ref": "https://hooks.example.com/x",
                                "connection_ref": "env:HZ_HOOK", "events": ["run.failed"]}))
    code, made = cli("notifications", "targets", "put", "ops", str(hub), "--file", str(body))
    assert code == 0 and made["type"] == "webhook" and made["enabled"] is False
    code, out = cli("notifications", "targets", "put", "ops", str(hub), "--file", str(body))
    assert code == 4  # exists: 'absent' no longer matches
    code, out = cli("notifications", "targets", "get", str(hub))
    assert [t["id"] for t in out["items"]] == ["ops"]
    code, out = cli("notifications", "deliveries", str(hub), "--target", "ops")
    assert code == 0 and out["items"] == []
    code, out = cli("notifications", "targets", "remove", "ops", str(hub), "--expected-revision", "99")
    assert code == 4
    code, out = cli("notifications", "targets", "remove", "ops", str(hub),
                    "--expected-revision", str(made["revision"]))
    assert code == 0 and out == {"deleted": "ops"}
