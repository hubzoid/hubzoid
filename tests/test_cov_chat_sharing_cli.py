"""`hubzoid chat shares|collaborators` (hubzoid.chat.sharing_cli), local
mode on a scratch hub: preview then create a link (a wrong digest refused),
list your links or the hub's (without tokens), change and revoke one, copy a
link into your own conversation, list and set collaborators (an unknown
person refused). Replies are written through the web app, as in
tests/test_cov_chat_engine_cli.py."""
from __future__ import annotations

import json

from tests.test_cov_chat_engine_cli import cli, client, created, hub, send  # noqa: F401


def test_links_from_preview_to_revoke(hub, client, tmp_path):  # noqa: F811
    cid, branch = created(hub)
    reply = send(client, cid, branch, "the plan")
    body = tmp_path / "preview.json"
    body.write_text(json.dumps({"conversation_id": cid, "head_message_id": reply["assistant_message_id"],
                                "audience": {"type": "deployment"}}))
    code, pv = cli("shares", "preview", str(hub), "--file", str(body))
    assert code == 0 and pv["projection"]["messages"][0]["content"][0]["text"] == "the plan"
    code, bad = cli("shares", "create", str(hub), "--preview", pv["preview_id"], "--digest", "sha256:00")
    assert code != 0 and bad["detail"]["code"] == "snapshot_changed"
    code, made = cli("shares", "create", str(hub), "--preview", pv["preview_id"], "--digest", pv["digest"])
    assert code == 0 and made["id"] and made["url"].startswith("/s/")      # no public URL set: a path
    token = made["url"].rsplit("/", 1)[1]

    code, mine = cli("shares", "list", str(hub), "--conversation", cid)
    assert code == 0 and [s["id"] for s in mine["items"]] == [made["id"]]
    code, every = cli("shares", "list", str(hub), "--all")
    assert code == 0 and made["id"] in [s["id"] for s in every["items"]]
    assert all("url" not in s or token not in str(s.get("url")) for s in every["items"])

    code, copy = cli("shares", "copy", token, str(hub))
    assert code == 0 and copy["new_conversation_id"] != cid

    change = tmp_path / "change.json"
    change.write_text(json.dumps({"allow_copy": False, "expected_revision": made["revision"]}))
    code, changed = cli("shares", "change", made["id"], str(hub), "--file", str(change))
    assert code == 0 and changed["allow_copy"] is False
    code, out = cli("shares", "revoke", made["id"], str(hub), "--expected-revision", str(changed["revision"]))
    assert code == 0 and out == {"revoked": made["id"]}
    code, gone = cli("shares", "copy", token, str(hub))
    assert code != 0 and gone["detail"]["code"] == "not_found"


def test_collaborators(hub, client, tmp_path):  # noqa: F811
    from hubzoid.auth import users

    users.create(hub, email="ana@example.org", name="Ana")
    cid, _branch = created(hub)
    code, listed = cli("collaborators", "list", str(hub), "--conversation", cid)
    assert code == 0 and listed["members"] == [] and listed["revision"] == 0 and listed["effective_role"] == "owner"
    members = tmp_path / "members.json"
    members.write_text(json.dumps({"members": [{"principal_id": "ana@example.org", "role": "viewer"}]}))
    code, out = cli("collaborators", "set", str(hub), "--conversation", cid, "--file", str(members),
                    "--expected-revision", "0")
    assert code == 0 and out["revision"] == 1 and out["members"][0]["email"] == "ana@example.org"
    members.write_text(json.dumps({"members": [{"principal_id": "nobody@example.org", "role": "viewer"}]}))
    code, err = cli("collaborators", "set", str(hub), "--conversation", cid, "--file", str(members),
                    "--expected-revision", "1")
    assert code != 0 and err["detail"]["code"] == "principal_not_found"
