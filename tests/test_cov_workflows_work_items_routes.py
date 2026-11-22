"""The work items, operations, stations and budgets API
(hubzoid.work_items.routes), mounted on a small app with a signed-in caller:
idempotent starts, reads, messages, cancel, refusals for unknown hubs and
items, cross-site writes from a browser session, and the budgets file."""
from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from hubzoid.work_items import routes, service

OWNER = "admin@localhost"
WORKFLOW = textwrap.dedent('''
    from hubzoid import hub, workflow
    from hubzoid.work_items import Route

    @workflow(work_items={"answer": Route(stages=("plan",), actions=())})
    def desk():
        return hub.work_item.checkpoint("plan", lambda: {"ok": True})
''')


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for key in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_IN_APP_EDITING"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "desk"
    (root / "workflows" / "desk").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
    (root / "workflows" / "desk" / "main.py").write_text(WORKFLOW)
    return root


def make_client(hub_dir, via="api-key"):
    actor = SimpleNamespace(subject=OWNER, surface="api", via=via, credential_id="k1")
    router = APIRouter()

    def person():
        return actor

    def hub_path(a, hub):
        if hub != "desk":
            raise KeyError(hub)
        return hub_dir

    routes.register(router, person, hub_path)
    routes.register_stations(router, person, hub_path)
    routes.register_budgets(router, person, hub_path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_start_is_idempotent_and_items_are_readable(hub_dir, monkeypatch):
    monkeypatch.setattr(service, "admit_and_start", lambda hub_dir, **kw: service.admit(
        hub_dir, workflow=kw["workflow"], key=kw["key"], route=kw["route"], input=kw["input"],
        requester=kw["requester"], generation_mode=kw["generation_mode"]))
    c = make_client(hub_dir)
    body = {"workflow": "desk", "key": "operator:1", "input": {"q": "hi"}}
    first = c.post("/hubs/desk/work-items", json=body, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 202, first.text
    item = first.json()
    assert item["status_url"] == f"/portal/api/hubs/desk/work-items/{item['id']}"
    again = c.post("/hubs/desk/work-items", json=body, headers={"Idempotency-Key": "k-1"})
    assert again.status_code == 202 and again.json()["id"] == item["id"]         # replayed response
    same = c.post("/hubs/desk/work-items", json=body, headers={"Idempotency-Key": "k-2"})
    assert same.status_code == 200 and same.json()["id"] == item["id"]           # same key: same item

    assert [i["id"] for i in c.get("/hubs/desk/work-items").json()["items"]] == [item["id"]]
    assert c.get(f"/hubs/desk/work-items/{item['id']}").json()["logical_key"] == "operator:1"
    assert c.get(f"/hubs/desk/work-items/{item['id']}/operations").json() == {"operations": []}
    assert c.get("/hubs/desk/work-items/config").json()["sources"] == []
    msg = c.post(f"/hubs/desk/work-items/{item['id']}/messages", json={"message": "also the refund"})
    assert msg.status_code == 202
    cancelled = c.post(f"/hubs/desk/work-items/{item['id']}/cancel", json={"reason": "duplicate"})
    assert cancelled.status_code == 202
    assert c.get(f"/hubs/desk/work-items/{item['id']}/stations").json()["calls"] == []
    assert c.get(f"/hubs/desk/work-items/{item['id']}/handoffs/h-1").status_code == 404


def test_refusals(hub_dir):
    c = make_client(hub_dir)
    assert c.get("/hubs/other/work-items").json()["detail"]["code"] == "hub_not_found"
    missing = c.get("/hubs/desk/work-items/wi-missing")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "not_found"
    assert c.get("/hubs/desk/operations").json() == {"operations": []}
    assert c.get("/hubs/desk/operations/op-missing").status_code == 404
    assert c.post("/hubs/desk/operations/op-missing/deny", json={}).status_code == 404
    approve = c.post("/hubs/desk/operations/op-missing/approve", json={"digest": "sha256:x"})
    assert approve.status_code == 404 and approve.json()["detail"]["code"] == "not_found"
    (hub_dir / "work-items.yaml").write_text("version: 9\n")
    bad = c.get("/hubs/desk/work-items/config")
    assert bad.status_code >= 400 and bad.json()["detail"]["code"]
    # A browser session must write from this site's own pages.
    browser = make_client(hub_dir, via="session")
    cross = browser.post("/hubs/desk/work-items/wi-x/messages", json={"message": "x"},
                         headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403


def test_budgets_file(hub_dir, monkeypatch):
    from hubzoid.access import store_for

    store_for(hub_dir).grant(OWNER, "desk", "workflows_manage", actor="t")
    c = make_client(hub_dir)
    shown = c.get("/hubs/desk/budgets").json()
    assert shown["yaml"] == ""
    assert c.post("/hubs/desk/budgets/validate", json={"yaml": "version: 7\n"}).status_code >= 400
    ok = c.post("/hubs/desk/budgets/validate", json={"yaml": "version: 1\n"}).json()
    assert ok["valid"] is True and ok["revision"]
    refused = c.put("/hubs/desk/budgets", json={"yaml": "version: 1\n", "expected_revision": "absent"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "in_app_editing_off"
    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "true")
    written = c.put("/hubs/desk/budgets", json={"yaml": "version: 1\n", "expected_revision": "absent"})
    assert written.status_code == 200, written.text
    assert (hub_dir / "budgets.yaml").read_text() == "version: 1\n"
    assert c.post("/hubs/desk/budgets/reservations/res-missing/release", json={"reason": "stuck"}).status_code == 404

    nobody = make_client(hub_dir)
    store_for(hub_dir).revoke(OWNER, "desk", "workflows_manage", actor="t")
    denied = nobody.get("/hubs/desk/budgets")
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "permission_denied"
