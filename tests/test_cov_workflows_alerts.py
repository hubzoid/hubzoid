"""Sending a workflow alert (hubzoid.workflows.alerts.send and deliver): the
signed webhook body, the Slack text, an email kept as a preview file, the
refusals for a missing destination, and what a failed delivery records (a
plain reason only for our own configuration errors, never a URL)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from hubzoid.workflows import alerts, events


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_ALERT_URL", "HUBZOID_ALERT_SECRET", "HUBZOID_PUBLIC_URL",
                "HUBZOID_EMAIL_DELIVERY", "HUBZOID_SMTP_FROM", "HUBZOID_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    h = tmp_path / "alpha"
    (h / "workflows").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: alpha\n---\nx")
    return h


def row(destination, aid="alert:abc123"):
    payload = {"hub": "alpha", "kind": "run_failed", "workflow": "nightly", "url": "https://hz.example/settings"}
    return {"id": aid, "destination": json.dumps(destination), "payload": json.dumps(payload)}


_REAL = httpx.AsyncClient


def capture(monkeypatch, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _REAL(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    return seen


def test_a_webhook_alert_is_signed_and_keyed(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ALERT_SECRET", "shh")
    seen = capture(monkeypatch)
    asyncio.run(alerts.send(hub, row({"webhook": "https://hooks.example/a"})))
    req = seen[0]
    assert str(req.url) == "https://hooks.example/a" and req.headers["Idempotency-Key"] == "alert:abc123"
    stamp = req.headers["X-Hubzoid-Timestamp"]
    want = "sha256=" + hmac.new(b"shh", stamp.encode() + b"." + req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Hubzoid-Signature"] == want
    assert json.loads(req.content)["kind"] == "run_failed"


def test_a_slack_alert_is_a_line_of_text_from_an_env_reference(hub, monkeypatch):
    monkeypatch.setenv("OPS_SLACK_URL", "https://hooks.slack.example/T1")
    seen = capture(monkeypatch)
    asyncio.run(alerts.send(hub, row({"slack": "OPS_SLACK_URL"})))
    body = json.loads(seen[0].content)
    assert set(body) == {"text"} and "run_failed: nightly" in body["text"] and "https://hz.example/settings" in body["text"]
    assert "X-Hubzoid-Signature" not in seen[0].headers


def test_missing_destinations_are_refused(hub, monkeypatch):
    with pytest.raises(ValueError, match="No alert destination configured"):
        asyncio.run(alerts.send(hub, row({"unconfigured": True})))
    with pytest.raises(ValueError, match="environment variable is missing"):
        asyncio.run(alerts.send(hub, row({"webhook": "NOT_SET_ANYWHERE"})))
    capture(monkeypatch, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(alerts.send(hub, row({"webhook": "https://hooks.example/a"})))


def test_an_email_alert_in_preview_mode_is_written_to_a_file(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    monkeypatch.setenv("HUBZOID_SMTP_FROM", "alerts@example.org")
    asyncio.run(alerts.send(hub, row({"email": "ops@example.org"}, aid="alert:feedbeef")))
    files = list(hub.rglob("alert-preview/feedbeef.eml"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "To: ops@example.org" in text and "Subject:" in text and "run_failed in alpha" in text
    with pytest.raises(ValueError):
        asyncio.run(alerts.send(hub, row({"email": "not-an-address"})))


def test_deliver_records_sent_and_failures_without_leaking_urls(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ALERT_URL", "https://hooks.example/default")
    alerts.record(hub, "alpha", "run_failed", "src-1", {"workflow": "nightly", "run_id": "r1"})
    with events._engine(hub).connect() as conn:
        from sqlalchemy import text

        aid = conn.execute(text("SELECT id FROM hz_workflow_alerts")).scalar_one()

    async def boom(hub_dir, r):
        raise httpx.ConnectError("could not reach https://hooks.example/secret-path")

    asyncio.run(alerts.deliver(hub, aid, sender=boom))
    failed = alerts._row(hub, aid)
    assert failed["state"] == "pending" and failed["attempt"] == 1
    assert failed["error"] == "ConnectError" and "secret-path" not in failed["error"]

    async def misconfigured(hub_dir, r):
        raise ValueError("Alert destination environment variable is missing")

    asyncio.run(alerts.deliver(hub, aid, sender=misconfigured))
    assert alerts._row(hub, aid)["error"] == "ValueError: Alert destination environment variable is missing"

    async def ok(hub_dir, r):
        return None

    asyncio.run(alerts.deliver(hub, aid, sender=ok))
    assert alerts._row(hub, aid)["state"] == "sent"
    asyncio.run(alerts.deliver(hub, aid, sender=boom))             # a sent alert is not sent again
    assert alerts._row(hub, aid)["state"] == "sent"
    assert alerts._where({"payload": "not json"}) == "?"
