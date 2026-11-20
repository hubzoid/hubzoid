"""The API reference lists the published API only (HUB-140 F9, D2), exactly the
listed routes, with their stability, the key operations that may call them and
the main shapes. A snapshot catches any change to the published contract."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"
SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi-public.json"
REFRESH = "HUBZOID_UPDATE_OPENAPI_SNAPSHOT"


@pytest.fixture
def client(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns(".hubzoid"))
    monkeypatch.delenv("HUBZOID_AUTH", raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid.server import build_app

    with TestClient(build_app(), base_url="http://127.0.0.1") as c:
        yield c


def test_the_reference_is_exactly_the_published_api(client):
    from hubzoid import __version__
    from hubzoid.openapi_doc import PUBLISHED

    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Hubzoid API" and schema["info"]["version"] == __version__
    listed = {(m.lower(), p) for p, ops in schema["paths"].items() for m in ops}
    # Every published route exists in the app, and nothing else is listed.
    assert listed == {(m.lower(), p) for m, p, _s in PUBLISHED}
    paths = set(schema["paths"])
    assert not [p for p in paths if p.startswith(("/internal", "/api/parity", "/api/admin", "/api/auth/mfa"))]
    assert "/v1/files" not in paths and not [p for p in paths if p.startswith("/api/chat/files/uploads")]
    assert {"/api/chat", "/api/runs/{run_ref}/cancel", "/portal/api/my/runs"} <= paths
    assert not [p for p in paths if p.startswith("/portal/") and not p.startswith(("/portal/api/my",
                "/portal/api/workflows", "/portal/api/runs", "/portal/api/hubs/{hub}/operations"))]
    assert schema["security"] == [{"personalKey": []}]
    assert client.get("/api/openapi.json").json()["paths"].keys() == schema["paths"].keys()


def test_each_route_says_who_may_call_it(client):
    schema = client.get("/openapi.json").json()
    ops = schema["paths"]
    assert ops["/api/requests/{rid}/decisions"]["post"]["x-hubzoid-key-operations"] == ["approve"]
    assert ops["/portal/api/hubs/{hub}/operations/{op_id}/approve"]["post"]["x-hubzoid-key-operations"] == ["approve"]
    assert ops["/v1/chat/completions"]["post"]["x-hubzoid-key-operations"] == ["chat", "manage"]
    assert ops["/api/requests"]["get"]["x-hubzoid-key-operations"] == ["read", "manage"]
    assert ops["/portal/api/my/runs"]["post"]["x-hubzoid-key-operations"] == ["manage"]
    assert all(op["x-hubzoid-stability"] in ("stable", "beta") for p in ops.values() for op in p.values())
    body = ops["/api/requests/{rid}/decisions"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/Decision"}
    assert "WebhookPayload" in schema["components"]["schemas"]
    events = ops["/api/conversations/{cid}/events"]["get"]["responses"]["200"]["content"]
    assert "text/event-stream" in events


def test_documented_bodies_are_accepted_by_the_real_handlers(client):
    from hubzoid.api_schemas import ConversationCreate, Operation

    agent = client.get("/v1/models", headers={"Authorization": "Bearer dev"}).json()["data"][0]["id"]
    body = ConversationCreate(agent=agent).model_dump(exclude_none=True)
    origin = {"Origin": "http://127.0.0.1"}
    made = client.post("/api/conversations", json=body, headers=origin)
    assert made.status_code == 201, made.text
    conv = made.json()
    op = Operation(kind="send", branch_id=conv["branch"]["id"],
                   content=[{"type": "text", "text": "hello"}]).model_dump(exclude_none=True)
    sent = client.post(f"/api/conversations/{conv['conversation']['id']}/operations", json=op,
                       headers={**origin, "Idempotency-Key": "doc-example-1"})
    assert sent.status_code == 202, sent.text
    assert sent.json().get("run_id")


def _contract(schema: dict) -> dict:
    """What callers rely on: per operation its stability, key operations,
    parameters and the shapes of its bodies (not descriptions or the version)."""
    comps = schema.get("components", {}).get("schemas", {})

    def shape(s, depth=0):
        if not isinstance(s, dict) or depth > 4:
            return None
        if "$ref" in s:
            return shape(comps.get(s["$ref"].rsplit("/", 1)[1], {}), depth + 1)
        out = {k: s[k] for k in ("type", "enum", "required") if k in s}
        if "anyOf" in s:
            out["anyOf"] = [shape(x, depth + 1) for x in s["anyOf"]]
        if "items" in s:
            out["items"] = shape(s["items"], depth + 1)
        if "properties" in s:
            out["properties"] = {k: shape(v, depth + 1) for k, v in s["properties"].items()}
        return out

    out = {}
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            out[f"{method.upper()} {path}"] = {
                "stability": op.get("x-hubzoid-stability"),
                "key_operations": op.get("x-hubzoid-key-operations"),
                "parameters": sorted((p["in"], p["name"], bool(p.get("required"))) for p in op.get("parameters", [])),
                "request": {ct: shape(c.get("schema")) for ct, c in
                            (op.get("requestBody", {}).get("content") or {}).items()},
                "responses": {code: {ct: shape(c.get("schema")) for ct, c in (r.get("content") or {}).items()}
                              for code, r in op.get("responses", {}).items() if code.startswith("2")},
            }
    return json.loads(json.dumps(out))


def _breaking(old: dict, new: dict) -> list[str]:
    msgs = []
    for key, was in old.items():
        now = new.get(key)
        if now is None:
            msgs.append(f"{key}: removed")
            continue
        if was["key_operations"] != now["key_operations"]:
            msgs.append(f"{key}: key operations {was['key_operations']} -> {now['key_operations']}")
        if was["stability"] == "stable" and now["stability"] != "stable":
            msgs.append(f"{key}: no longer stable")
        if was["parameters"] != now["parameters"]:
            msgs.append(f"{key}: parameters changed")
        for ct, s in (now["request"] or {}).items():
            added = set((s or {}).get("required", [])) - set(((was["request"] or {}).get(ct) or {}).get("required", []))
            if added:
                msgs.append(f"{key}: newly required request fields {sorted(added)}")
        for code, contents in (was["responses"] or {}).items():
            for ct, s in contents.items():
                gone = set((s or {}).get("properties", {})) - set(
                    (((now["responses"] or {}).get(code) or {}).get(ct) or {}).get("properties", {}))
                if gone:
                    msgs.append(f"{key}: response fields removed {sorted(gone)}")
    return msgs


def test_the_published_contract_matches_the_snapshot(client):
    contract = _contract(client.get("/openapi.json").json())
    if os.environ.get(REFRESH) or not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n")
        if not os.environ.get(REFRESH):
            pytest.fail(f"Wrote a new snapshot at {SNAPSHOT}; commit it.")
        return
    saved = json.loads(SNAPSHOT.read_text())
    if saved == contract:
        return
    breaking = _breaking(saved, contract)
    added = sorted(set(contract) - set(saved))
    pytest.fail("The published API changed.\n"
                + ("Breaking for callers (stable routes need a changelog entry and one release of overlap):\n  "
                   + "\n  ".join(breaking) + "\n" if breaking else "")
                + (f"Added: {added}\n" if added else "")
                + f"If this is intended, refresh the snapshot: {REFRESH}=1 pytest tests/test_openapi_doc.py")
