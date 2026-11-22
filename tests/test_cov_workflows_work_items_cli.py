"""`hubzoid work-items` and `hubzoid operations`: validate, list, show, message,
cancel and the operation views from the CLI, in JSON and for people, with the
refusals and exit codes. Items are admitted through the service (no workflow
engine), as in tests/test_work_items.py."""
from __future__ import annotations

import json
import textwrap

import pytest
from typer.testing import CliRunner

from hubzoid import db
from hubzoid.cli import app
from hubzoid.work_items import operations, service, store
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity

OWNER = "admin@localhost"
WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"answer": Route(stages=("plan",), actions=()),
                          "ops": Route()})
    def desk():
        return hub.work_item.checkpoint("plan", lambda: {"ok": True})
''')


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for key in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "desk"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    return root


def wi(*args, as_json=True, group="work-items"):
    r = CliRunner().invoke(app, [group, *args, *(["--json"] if as_json else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if as_json and r.output.strip() else r.output)


def test_validate_and_config(hub_dir):
    code, out = wi("validate", str(hub_dir))
    assert code == 0 and out["ok"] is True and out["present"] is False
    assert out["workflows"]["desk"] and set(out["workflows"]["desk"]) >= {"answer", "ops"}
    code, text = wi("validate", str(hub_dir), as_json=False)
    assert code == 0 and "no work-items.yaml (defaults)" in text and "workflow desk" in text

    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes: {email.send: never}\n")
    code, out = wi("validate", str(hub_dir))
    assert code == 2 and "off, draft or approve" in out["detail"]["message"]
    (hub_dir / "work-items.yaml").write_text("version: 1\nwrite_modes: {email.send: draft}\n")
    code, out = wi("validate", str(hub_dir))
    assert out["present"] is True and out["config"]["write_modes"]["email.send"] == "draft"
    code, text = wi("validate", str(hub_dir), as_json=False)
    assert "email.send: draft" in text
    code, out = wi("config", str(hub_dir))
    assert code == 0 and out["effective"]["write_modes"]["email.send"] == "draft"
    assert out["sources"] == ["work-items.yaml"] and out["revision"]

    (hub_dir / "work-items.yaml").write_text("version: 9\n")
    code, out = wi("validate", str(hub_dir))
    assert code == 2 and out["detail"]["code"]

    code, text = wi("validate", str(hub_dir / "absent"), as_json=False)
    assert code == 2 and "No hub folder" in text


def test_list_show_message_and_cancel(hub_dir):
    item, _ = service.admit(hub_dir, workflow="desk", key="operator:demo-1", input={"message": "hi"},
                            requester=OWNER)
    code, listed = wi("list", str(hub_dir))
    assert code == 0 and [i["id"] for i in listed["items"]] == [item["id"]]
    code, text = wi("list", str(hub_dir), as_json=False)
    assert code == 0 and item["id"] in text and "operator:demo-1" in text
    code, listed = wi("list", str(hub_dir), "--state", "completed")
    assert listed["items"] == []

    code, shown = wi("show", str(hub_dir), item["id"])
    assert code == 0 and shown["logical_key"] == "operator:demo-1" and shown["route"] == "answer"
    code, text = wi("show", str(hub_dir), item["id"], as_json=False)
    assert code == 0 and "operator:demo-1" in text and "generation 1" in text

    code, out = wi("message", str(hub_dir), item["id"], "--text", "also check the refund")
    assert code == 0, out
    from hubzoid.work_items.facade import WorkItem

    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    with context.run_scope(hub="desk", workflow="desk", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           identity=ident, run_id="run-1",
                           work={"item": store.get_item(hub_dir, item["id"]), "stage": None}):
        assert [m["text"] for m in WorkItem().messages()] == ["also check the refund"]

    code, out = wi("cancel", str(hub_dir), item["id"], "--reason", "duplicate")
    assert code == 0, out
    code, shown = wi("show", str(hub_dir), item["id"])
    assert shown["state"] in ("cancelled", "cancelling")

    code, out = wi("show", str(hub_dir), "wi-missing")
    assert code == 2 and out["detail"]["code"] == "not_found"


def test_operations_list_show_and_decisions(hub_dir):
    item = service.admit(hub_dir, workflow="desk", key="ops-1", route="ops", requester=OWNER)[0]
    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    with context.run_scope(hub="desk", workflow="desk", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           identity=ident, run_id="run-1", work={"item": item, "stage": None}):
        operations.run(name="probe", target={"n": 1}, arguments={}, recovery="idempotent",
                       actions="probe.call", dispatch=lambda args, **kw: {"ok": True})
    op = store.list_ops(hub_dir, "desk")[0]

    code, listed = wi("list", str(hub_dir), group="operations")
    assert code == 0 and op["id"] in json.dumps(listed)
    code, text = wi("list", str(hub_dir), as_json=False, group="operations")
    assert code == 0 and op["id"] in text
    code, detail = wi("show", str(hub_dir), op["id"], group="operations")
    assert code == 0 and detail["id"] == op["id"] and detail["name"] == "probe"
    code, out = wi("show", str(hub_dir), "op-missing", group="operations")
    assert code == 2 and out["detail"]["code"] == "not_found"

    # A sent operation is not waiting for a decision.
    code, out = wi("approve", str(hub_dir), op["id"], "--digest", op["args_digest"], group="operations")
    assert code in (2, 4) and out["detail"]["code"]
