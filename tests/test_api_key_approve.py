"""The ``approve`` key operation: opt-in, in no preset, the only way a key
decides approvals and work-item operations, and recorded as a key decision."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hubzoid import auth
from hubzoid.access.session import require_same_origin
from hubzoid.auth import api_keys, routes
from hubzoid.hitl import routes as hitl_routes
from hubzoid.hitl import service as hitl

from tests.test_auth_mfa import hub  # noqa: F401 - fixture
from tests.test_auth_routes import ORIGIN, person, sign_in


@pytest.mark.parametrize("ops,method,path,ok", [
    (("manage",), "POST", "/api/requests/r1/decisions", False),
    (("chat", "read", "manage"), "POST", "/api/requests/r1/decisions", False),
    (("approve",), "POST", "/api/requests/r1/decisions", True),
    (("approve",), "GET", "/api/requests/r1/decisions", False),
    (("manage",), "POST", "/portal/api/hubs/h/operations/o1/approve", False),
    (("manage",), "POST", "/portal/api/hubs/h/operations/o1/deny", False),
    (("manage",), "POST", "/portal/api/hubs/h/operations/o1/resolve", False),
    (("approve",), "POST", "/portal/api/hubs/h/operations/o1/approve", True),
    (("approve",), "POST", "/portal/api/change-requests/r1/confirm", False),
    (("manage",), "POST", "/api/requests/r1/cancel", True),
    (("approve",), "POST", "/api/hub-changes/1/decision", False),
])
def test_only_approve_reaches_decisions(ops, method, path, ok):
    assert api_keys.allows(ops, method, path) is ok


def test_approve_is_in_no_preset():
    assert all("approve" not in ops for ops in api_keys.PRESETS.values())
    assert "approve" in api_keys.OPERATIONS


def _app(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)

    async def user(request: Request):
        return auth.require_user(request, hub)

    async def mutation(request: Request):
        require_same_origin(request)
        return auth.require_user(request, hub)

    hitl_routes.mount(app, SimpleNamespace(hub_dir=hub, hub="sales", user=user, mutation=mutation))
    return TestClient(app)


def _key(c, **body) -> str:
    r = c.post("/api/me/api-keys", json={"name": "bot", **body}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _pending(hub, user) -> dict:
    return hitl.create(hub, "sales", hitl.Caller(account_id=user["id"], email=user["email"], origin="api"),
                       kind="approval", title="Reserve 4 units", tool="reserve_stock",
                       arguments={"sku": "SKU-12", "quantity": 4})


def test_an_approve_key_decides_and_is_recorded(hub):
    user = person(hub)
    c = _app(hub)
    sign_in(c)
    manage = _key(c, preset="all")
    approver = _key(c, operations=["read", "approve"])
    listed = c.get("/api/me/api-keys").json()
    assert any(k["operations"] == ["approve", "read"] for k in listed["api_keys"])
    req = _pending(hub, user)
    bot = TestClient(c.app)
    no = bot.post(f"/api/requests/{req['id']}/decisions", headers={"authorization": f"Bearer {manage}"},
                  json={"decision": "approve"})
    assert no.status_code == 403 and no.json()["detail"]["code"] == "key_not_allowed"
    ok = bot.post(f"/api/requests/{req['id']}/decisions", headers={"authorization": f"Bearer {approver}"},
                  json={"decision": "approve", "digest": req["digest"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"
    stored = hitl.get(hub, "sales", req["id"])
    assert stored["decided_via"] == "api_key" and stored["decided_by"] == user["id"]
    from hubzoid.hitl import store as st

    with hitl.engine(hub).connect() as conn:
        events = [dict(r) for r in conn.execute(st.events.select().where(st.events.c.request_id == req["id"])).mappings()]
    settled = [e for e in events if e["type"] == "request.settled"]
    assert settled and '"via": "api_key"' in settled[0]["payload_json"]
    key_id = approver.split("_")[1]
    assert key_id in settled[0]["payload_json"]


def test_hub_changes_stay_person_only(hub):
    user = person(hub)
    c = _app(hub)
    sign_in(c)
    approver = _key(c, operations=["approve"])

    class R:
        headers = {"authorization": f"Bearer {approver}"}

    keyed = SimpleNamespace(credential="api_key", operations=("approve",), via_key=True)
    with pytest.raises(Exception) as err:
        hitl_routes.human_only(R(), keyed)
    assert "human_action_required" in str(err.value.detail)
    assert hitl_routes.decision_credential(R(), keyed) == "api_key"
    assert user["id"]


def test_a_machine_credential_never_decides():
    class R:
        headers = {"authorization": "Bearer eyJ.a.b"}

    machine = SimpleNamespace(credential="app_jwt", operations=("chat", "read", "approve"), via_key=True)
    with pytest.raises(Exception) as err:
        hitl_routes.decision_credential(R(), machine)
    assert "human_action_required" in str(err.value.detail)
    assert auth.automated("app_jwt") and auth.automated("api_key") and not auth.automated("session")
