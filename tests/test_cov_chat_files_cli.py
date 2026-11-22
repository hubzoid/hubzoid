"""`hubzoid files` and `hubzoid artifacts`: upload, wait, attach, download,
references and delete a chat file from the CLI, the storage doctor, and the
refusals for missing files and conversations."""
from __future__ import annotations

import hashlib
import json

from typer.testing import CliRunner

from hubzoid.cli import app
from tests.test_cov_chat_engine_cli import created, hub  # noqa: F401


def run(*args):
    r = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
    return r.exit_code, json.loads(r.output)


def test_upload_attach_download_and_delete(hub, tmp_path):  # noqa: F811
    cid, _ = created(hub)
    src = tmp_path / "notes.txt"
    src.write_text("Quarter close checklist\n- reconcile bank\n")

    code, out = run("files", "upload", str(tmp_path / "absent.txt"), "--hub", str(hub))
    assert code == 2 and "is not a file" in out["detail"]["message"]

    code, desc = run("files", "upload", str(src), "--hub", str(hub), "--conversation", cid)
    assert code == 0, desc
    fid = desc["file_id"]
    assert desc["attached"]

    code, ready = run("files", "wait", fid, "--hub", str(hub), "--timeout", "10")
    assert code == 0 and ready["state"] == "ready"
    code, status = run("files", "status", fid, "--hub", str(hub))
    assert code == 0 and status["file_id"] == fid

    code, listed = run("files", "list", str(hub), "--conversation", cid)
    assert code == 0 and fid in json.dumps(listed)
    code, preview = run("files", "preview", fid, "--hub", str(hub))
    assert code == 0 and isinstance(preview, dict)

    target = tmp_path / "copy.txt"
    code, saved = run("files", "download", fid, "--output", str(target), "--hub", str(hub))
    assert code == 0 and target.read_bytes() == src.read_bytes()
    assert saved["content_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()

    code, refs = run("files", "references", fid, "--hub", str(hub))
    assert code == 0 and refs["items"]
    ref = refs["items"][0]
    ref_id = ref.get("id") or ref.get("reference_id")
    code, out = run("files", "detach", fid, "--reference", ref_id, "--hub", str(hub))
    assert code == 0 and out == {"detached": ref_id}
    code, refs = run("files", "references", fid, "--hub", str(hub))
    assert ref_id not in json.dumps(refs["items"])

    code, gone = run("files", "delete", fid, "--hub", str(hub))
    assert code == 0 and gone == {"file_id": fid, "state": "deleted"}
    code, out = run("files", "status", fid, "--hub", str(hub))
    assert code == 2 and out["detail"]["code"] == "file_not_found"


def test_attach_refusals_and_doctor(hub, tmp_path):  # noqa: F811
    src = tmp_path / "a.txt"
    src.write_text("x")
    code, desc = run("files", "upload", str(src), "--hub", str(hub))
    assert code == 0
    code, out = run("files", "attach", desc["file_id"], "--conversation", "c_missing", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"]
    code, out = run("files", "status", "f-missing", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "file_not_found"

    code, report = run("files", "doctor", str(hub))
    assert code == 0 and report["ok"] is True


def test_artifacts_of_a_conversation(hub, tmp_path):  # noqa: F811
    cid, _ = created(hub)
    code, out = run("artifacts", "list", str(hub), "--conversation", cid)
    assert code == 0 and out["items"] == []
    code, out = run("artifacts", "show", "art-missing", "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
    code, out = run("artifacts", "download", "art-missing", "--output", str(tmp_path / "x"), "--hub", str(hub))
    assert code != 0 and out["detail"]["code"] == "not_found"
