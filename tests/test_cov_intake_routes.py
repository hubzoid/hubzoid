"""Intake review over HTTP (hubzoid.intake.routes), with the service
replaced: reading needs intake.read and deciding intake.manage (as a write);
release, reject and retry need a JSON object with expected_revision (retry
takes no reason); an attachment from outside is always a download with a
cleaned file name, never rendered; the service's refusals pass through with
their status. The intake service has its own tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hubzoid.channels.errors import ChannelError
from hubzoid.control import authz
from hubzoid.intake import routes, service

BASE = "/api/hubs/desk/intake"
ITEM = {"id": "it-1", "state": "held", "revision": 3}


@pytest.fixture
def api(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nDesk.")
    asked, calls = [], []

    async def require(request, hub_dir, capability, hub, *, mutation=False):
        asked.append((capability, mutation))
        return SimpleNamespace(email="ops@example.org")

    def recorder(name, result=ITEM):
        def fn(hub_dir, *a, **kw):
            calls.append((name, a, kw))
            if isinstance(result, Exception):
                raise result
            return result
        return fn

    monkeypatch.setattr(authz, "require_request", require)
    monkeypatch.setattr(service, "list_items", recorder("list", {"items": [ITEM], "next_cursor": None}))
    monkeypatch.setattr(service, "require", recorder("require"))
    monkeypatch.setattr(service, "view", lambda item, full=False, hub_dir=None: {**item, "full": full})
    for name in ("release", "reject", "retry"):
        monkeypatch.setattr(service, name, recorder(name, {**ITEM, "state": name}))
    app = FastAPI()
    routes.mount(app, root)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, asked=asked, calls=calls, recorder=recorder)


def test_reading_needs_intake_read(api):
    r = api.client.get(BASE, params={"channel": "mail", "state": "held", "limit": 5})
    assert r.status_code == 200 and r.json()["items"] == [ITEM] and r.headers["cache-control"] == "no-store"
    assert api.calls[-1] == ("list", (), {"channel": "mail", "state": "held", "cursor": None, "limit": 5})
    one = api.client.get(f"{BASE}/it-1").json()
    assert one["full"] is True and one["id"] == "it-1"
    assert api.asked == [("intake.read", False), ("intake.read", False)]


def test_deciding_needs_intake_manage_and_a_revision(api):
    r = api.client.post(f"{BASE}/it-1/release", json={"expected_revision": 3, "reason": "known customer"})
    assert r.status_code == 200 and r.json()["state"] == "release"
    assert api.calls[-1] == ("release", ("it-1",), {"actor": "ops@example.org", "expected_revision": 3,
                                                    "reason": "known customer"})
    api.client.post(f"{BASE}/it-1/reject", json={"expected_revision": 3})
    assert api.calls[-1][2]["reason"] == ""
    api.client.post(f"{BASE}/it-1/retry", json={"expected_revision": 3, "reason": "ignored"})
    assert "reason" not in api.calls[-1][2]
    assert set(api.asked) == {("intake.manage", True)}
    for bad in ({}, {"expected_revision": "three"}):
        r = api.client.post(f"{BASE}/it-1/release", json=bad)
        assert r.status_code == 422 and "expected_revision is required" in r.text
    for raw in ("not json", "[1, 2]"):
        r = api.client.post(f"{BASE}/it-1/release", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 422 and "Send a JSON object." in r.text


def test_refusals_pass_through_and_attachments_are_downloads(api, monkeypatch):
    monkeypatch.setattr(service, "release", api.recorder("release", ChannelError("stale", "It changed.", status=409)))
    r = api.client.post(f"{BASE}/it-1/release", json={"expected_revision": 2})
    assert r.status_code == 409 and "It changed." in r.text
    monkeypatch.setattr(service, "attachment_bytes", lambda hub_dir, aid: (
        {"filename": '../../evil"; name=x.html'}, b"<script>alert(1)</script>"))
    att = api.client.get(f"{BASE}/it-1/attachments/a-1")
    assert att.status_code == 200 and att.content == b"<script>alert(1)</script>"
    assert att.headers["content-type"] == "application/octet-stream" and att.headers["x-content-type-options"] == "nosniff"
    assert att.headers["content-disposition"] == 'attachment; filename="....evil namex.html"'
    monkeypatch.setattr(service, "attachment_bytes", lambda hub_dir, aid: ({"filename": "///"}, b"x"))
    assert api.client.get(f"{BASE}/it-1/attachments/a-2").headers["content-disposition"] == \
        'attachment; filename="attachment"'
