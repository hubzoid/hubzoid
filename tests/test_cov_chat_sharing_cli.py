"""`hubzoid chat collaborators` (hubzoid.chat.sharing_cli), local mode on a
scratch hub: list and set collaborators (an unknown person refused). The
links commands need a finished reply, which can race without the run status
fix in #163: those tests wait for it."""
from __future__ import annotations

import json

from tests.test_cov_chat_engine_cli import cli, client, created, hub  # noqa: F401


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
