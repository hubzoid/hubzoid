"""Bounded historical reads retain the existing admission and restart rules."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from hubzoid.channels import config
from hubzoid.channels.email import history, poller, transports
from hubzoid.channels.errors import ChannelError
from tests.test_channels_email import (  # reuse the actual Gmail/intake harness
    _consent, gmail_conn, google, hub_dir, items, message, route_all, started,
)


@pytest.fixture
def mailbox(hub_dir, google, started, monkeypatch):
    conn = gmail_conn(hub_dir)
    _consent(hub_dir, conn)
    for n in range(3):
        google.arrive(message(subject=f"invoice {n}", msgid=f"<history-{n}@shop.example>"))
    calls = []
    invalid_page = [False]
    timestamp = str(int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp() * 1000))

    def respond(request):
        calls.append(request)
        if request.url.path == "/gmail/v1/users/me/messages" and "in:sent" not in request.url.params.get("q", ""):
            params = request.url.params
            assert params["labelIds"] == "INBOX"
            assert params["q"].startswith("(label:orders) after:")
            if params.get("pageToken"):
                if invalid_page[0]:
                    return httpx.Response(400, json={"error": "expired page"})
                return httpx.Response(200, json={"messages": [{"id": "m3"}]})
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}],
                                            "nextPageToken": "page-2"})
        response = google(request)
        if "/messages/" in request.url.path and request.method == "GET" and response.status_code == 200:
            return httpx.Response(200, json={**response.json(), "internalDate": timestamp})
        return response

    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(respond)))
    return conn, google, calls, invalid_page


def run(conn, **kw):
    return history.import_history(conn, query="label:orders", after="2026-10-01", before="2026-11-01", **kw)


def test_preview_binds_scope_without_reading_mail_and_rejects_changed_scope(mailbox):
    conn, _, calls, _ = mailbox
    plan = run(conn)
    assert plan["state"] == "preview" and not calls
    assert not config.active(conn.hub_dir, conn.id).state.get("email_import")
    with pytest.raises(ChannelError, match="digest does not match"):
        history.import_history(conn, query="label:orders", after="2026-09-01", before="2026-11-01",
                               apply_digest=plan["digest"])
    assert not calls


def test_pages_resume_through_existing_admission_and_keep_new_mail_cursor(mailbox):
    conn, google, _, _ = mailbox
    digest = run(conn)["digest"]
    first = run(conn, apply_digest=digest)
    assert first["processed"] == 2 and first["state"] == "running"
    initial_cursor = config.active(conn.hub_dir, conn.id).state["email_cursor"]
    google.arrive(message(subject="new arrival", msgid="<new-arrival@shop.example>"))
    second = run(conn, apply_digest=digest)
    assert second["processed"] == 3 and second["state"] == "complete"
    assert config.active(conn.hub_dir, conn.id).state["email_cursor"] == initial_cursor
    assert run(conn, apply_digest=digest)["messages"] == 0
    assert poller.poll_once(conn)["messages"] == 1
    route_all(conn.hub_dir)
    assert len(items(conn.hub_dir)) == 4
    assert not google.sent and not google.drafts


def test_interrupted_page_repeats_only_uncheckpointed_message(mailbox, monkeypatch):
    conn, _, _, _ = mailbox
    real = poller.process
    seen = []

    def process(c, m):
        seen.append(m["id"])
        if m["id"] == "gmail:m2" and seen.count("gmail:m2") == 1:
            raise RuntimeError("interrupted")
        return real(c, m)

    monkeypatch.setattr(poller, "process", process)
    digest = run(conn)["digest"]
    with pytest.raises(RuntimeError, match="interrupted"):
        run(conn, apply_digest=digest)
    assert run(conn, apply_digest=digest)["processed"] == 2
    assert run(conn, apply_digest=digest)["state"] == "complete"
    assert seen == ["gmail:m1", "gmail:m2", "gmail:m2", "gmail:m3"]
    route_all(conn.hub_dir)
    assert len(items(conn.hub_dir)) == 3


def test_expired_page_needs_explicit_same_scope_restart_and_limit_is_honest(mailbox):
    conn, _, _, expired = mailbox
    digest = run(conn)["digest"]
    run(conn, apply_digest=digest)
    expired[0] = True
    with pytest.raises(ChannelError) as exc:
        run(conn, apply_digest=digest)
    assert exc.value.code == "pagination_expired"
    assert run(conn, apply_digest=digest, restart=True)["processed"] == 2
    expired[0] = False
    assert run(conn, apply_digest=digest)["state"] == "complete"
    bounded = run(conn, limit=1)["digest"]
    out = run(conn, limit=1, apply_digest=bounded)
    assert out["state"] == "limit_reached" and out["processed"] == 1
    assert run(conn, limit=1, apply_digest=bounded)["messages"] == 0


def test_rechecks_connection_and_respects_shared_poll_lease(mailbox):
    conn, _, calls, _ = mailbox
    digest = run(conn)["digest"]
    assert poller._lease(conn, "another-reader", 120)
    assert run(conn, apply_digest=digest)["state"] == "busy" and not calls
    poller._release(conn, "another-reader")
    changed = conn.config.model_dump()
    changed["provider_config"]["gmail"]["label_ids"] = ["OTHER"]
    config.apply(conn.hub_dir, changed, expected_revision=conn.revision, actor="test")
    with pytest.raises(ChannelError):
        run(conn, apply_digest=digest)
    assert not calls


def test_deleted_and_outside_period_mail_is_not_admitted(mailbox, monkeypatch):
    conn, _, _, _ = mailbox
    previous = transports._client

    def response(request):
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(404)
        out = previous.send(request)
        if request.url.path.endswith("/messages/m2"):
            return httpx.Response(200, json={**out.json(), "internalDate": str(history._epoch("2026-11-01") * 1000)})
        if request.url.path.endswith("/messages/m3"):
            return httpx.Response(200, json={**out.json(), "internalDate": str(history._epoch("2026-10-01") * 1000)})
        return out

    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(response)))
    digest = run(conn)["digest"]
    first = run(conn, apply_digest=digest)
    assert first["messages"] == 0 and first["deleted"] == 1 and first["outside_period"] == 1
    assert run(conn, apply_digest=digest)["state"] == "complete"
    route_all(conn.hub_dir)
    assert len(items(conn.hub_dir)) == 1


def test_provider_throttle_preserves_completed_message_and_scope(mailbox, monkeypatch):
    conn, _, _, _ = mailbox
    previous = transports._client
    throttled = [False]

    def response(request):
        if request.url.path.endswith("/messages/m2") and not throttled[0]:
            throttled[0] = True
            return httpx.Response(429, json={"private_provider_detail": "never display this body"})
        return previous.send(request)

    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(response)))
    digest = run(conn)["digest"]
    with pytest.raises(ChannelError) as exc:
        run(conn, apply_digest=digest)
    assert exc.value.code == "rate_limited" and "private_provider_detail" not in str(exc.value)
    assert run(conn, apply_digest=digest)["processed"] == 2
    assert run(conn, apply_digest=digest)["state"] == "complete"
