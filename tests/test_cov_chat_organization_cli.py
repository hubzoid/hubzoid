"""`hubzoid chat organization|folders|tags|bookmarks|organize|search|exports|imports`:
personal organisation, search and portability from the local CLI, with refusals."""
from __future__ import annotations

import json

from tests.test_cov_chat_engine_cli import cli, client, created, hub, send  # noqa: F401


def body(tmp_path, name, data):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_folders_tags_bookmarks_and_organize(hub, client, tmp_path):  # noqa: F811
    cid, branch = created(hub)
    sent = send(client, cid, branch, "where are the invoices?")

    code, folder = cli("folders", "create", str(hub), "--file", body(tmp_path, "f", {"name": "Finance"}))
    assert code == 0 and folder["name"] == "Finance"
    code, dup = cli("folders", "create", str(hub), "--file", body(tmp_path, "f2", {"name": "finance"}))
    assert code != 0 and dup["detail"]["code"]  # names are unique, ignoring case
    code, renamed = cli("folders", "update", folder["id"], str(hub),
                        "--file", body(tmp_path, "u", {"name": "Accounts", "expected_revision": folder["revision"]}))
    assert code == 0 and renamed["name"] == "Accounts"
    code, stale = cli("folders", "update", folder["id"], str(hub),
                      "--file", body(tmp_path, "u2", {"name": "X", "expected_revision": folder["revision"]}))
    assert code == 4
    code, listed = cli("folders", "list", str(hub))
    assert [f["name"] for f in listed["items"]] == ["Accounts"]
    code, ctx = cli("folders", "context", folder["id"], str(hub))
    assert code == 0 and isinstance(ctx, dict)

    code, out = cli("tags", "create", str(hub), "--name", "urgent", "--color", "red")
    assert code == 2 and "color is one of" in out["detail"]["message"]
    code, tag = cli("tags", "create", str(hub), "--name", "urgent", "--color", "rose")
    assert code == 0 and tag["name"] == "urgent" and tag["color"] == "rose"
    code, listed = cli("tags", "list", str(hub))
    assert [t["name"] for t in listed["items"]] == ["urgent"]

    code, placed = cli("organize", cid, str(hub),
                       "--file", body(tmp_path, "o", {"folder_id": folder["id"], "tag_ids": [tag["id"]],
                                                      "pinned_position": 1}))
    assert code == 0 and placed["folder_id"] == folder["id"]
    code, bad = cli("organize", cid, str(hub), "--file", body(tmp_path, "o2", {"folder_id": "no-such-folder"}))
    assert code != 0 and bad["detail"]["code"] == "not_found"

    code, mark = cli("bookmarks", "create", str(hub), "--conversation", cid,
                     "--message", sent["assistant_message_id"], "--label", "Invoice answer")
    assert code == 0 and mark["label"] == "Invoice answer"
    code, listed = cli("bookmarks", "list", str(hub))
    assert [b["id"] for b in listed["items"]] == [mark["id"]]
    code, gone = cli("bookmarks", "delete", mark["id"], str(hub), "--expected-revision", str(mark["revision"]))
    assert code == 0 and gone == {"deleted": mark["id"]}

    code, gone = cli("tags", "delete", tag["id"], str(hub), "--expected-revision", str(tag["revision"]))
    assert code == 0 and gone == {"deleted": tag["id"]}
    code, now = cli("folders", "list", str(hub))
    code, gone = cli("folders", "delete", folder["id"], str(hub),
                     "--expected-revision", str(now["items"][0]["revision"]))
    assert code == 0 and gone == {"deleted": folder["id"]}


def test_bulk_changes_and_manifest(hub, client, tmp_path):  # noqa: F811
    cid, branch = created(hub)
    send(client, cid, branch, "hello")
    code, out = cli("organization", "bulk", str(hub), "--file", body(tmp_path, "b", {"action": "explode", "items": []}))
    assert code == 2 and "action is" in out["detail"]["message"]
    code, out = cli("organization", "bulk", str(hub), "--file", body(tmp_path, "b0", {"action": "archive", "items": []}))
    assert code == 2 and "Choose 1 to" in out["detail"]["message"]
    code, out = cli("organization", "bulk", str(hub),
                    "--file", body(tmp_path, "b1", {"action": "archive", "items": [{"id": "c_missing"}]}))
    assert code != 0 and out["detail"]["code"] == "resource_not_found"
    code, out = cli("organization", "bulk", str(hub),
                    "--file", body(tmp_path, "b2", {"action": "archive", "items": [{"id": cid}]}))
    assert code == 0, out

    code, exported = cli("organization", "export", str(hub))
    assert code == 0 and "revision" in exported and "folders" in exported

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\nscope: self\n")
    code, out = cli("organization", "apply", str(hub), "--file", str(bad))
    assert code == 2 and "version: 1 and scope: self" in out["detail"]["message"]
    bad.write_text("version: 1\nscope: self\ncolour: blue\n")
    code, out = cli("organization", "apply", str(hub), "--file", str(bad))
    assert code == 2 and "Unknown manifest key 'colour'" in out["detail"]["message"]

    manifest = tmp_path / "org.yaml"
    manifest.write_text("version: 1\nscope: self\nfolders:\n  - id: fold-ops\n    name: Operations\n")
    code, dry = cli("organization", "apply", str(hub), "--file", str(manifest))
    assert code == 0 and dry["digest"], dry
    code, applied = cli("organization", "apply", str(hub), "--file", str(manifest), "--apply", "--digest", dry["digest"])
    assert code == 0, applied
    code, folders = cli("folders", "list", str(hub))
    assert "Operations" in [f["name"] for f in folders["items"]]


def test_search_and_portability(hub, client, tmp_path):  # noqa: F811
    cid, branch = created(hub)
    send(client, cid, branch, "the quarterly reconciliation is due on Friday")

    code, status = cli("search", "status", str(hub))
    assert code == 0 and isinstance(status, dict)
    code, rebuilt = cli("search", "rebuild", str(hub))
    assert code == 0 and isinstance(rebuilt, dict)
    code, found = cli("search", "query", "reconciliation", str(hub))
    assert code == 0 and cid in json.dumps(found)

    package = tmp_path / "export.zip"
    code, saved = cli("exports", "create", str(hub), "--conversation", cid, "--output", str(package))
    assert code == 0 and saved["bytes"] == package.stat().st_size > 0

    code, preview = cli("imports", "preview", str(hub), "--file", str(package))
    assert code == 0 and preview["digest"] and preview["objects"]
    code, out = cli("imports", "apply", str(hub), "--preview", preview["id"], "--digest", preview["digest"])
    assert code == 2 and "Add --apply" in out["detail"]["message"]
    code, out = cli("imports", "apply", str(hub), "--preview", preview["id"], "--digest", "sha256:other", "--apply")
    assert code == 4 and out["detail"]["code"] == "revision_conflict"
    code, result = cli("imports", "apply", str(hub), "--preview", preview["id"], "--digest", preview["digest"],
                       "--apply")
    assert code == 0, result
    code, again = cli("imports", "apply", str(hub), "--preview", preview["id"], "--digest", preview["digest"],
                      "--apply")
    assert code == 0 and again == result  # applying the same preview twice returns the first result
