"""Mailbox transport TLS setup, recovery cursors and honest send failure outcomes."""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from hubzoid.channels.email import oauth, transports as t
from hubzoid.channels.errors import ChannelError
from hubzoid.work_items.errors import NotSent, Rejected
from tests.test_channels_email import Box, FakeImap


@pytest.fixture
def conn(tmp_path):
    imap = SimpleNamespace(host="imap.example.test", port=993, tls="implicit", username="desk@example.org",
                           password_ref="env:TEST_MAIL_PASSWORD", folder="INBOX", drafts_folder=None)
    smtp = SimpleNamespace(host="smtp.example.test", port=465, tls="implicit", username="desk@example.org",
                           password_ref="env:TEST_MAIL_PASSWORD")
    return SimpleNamespace(id="mail", hub="desk", hub_dir=tmp_path, settings=SimpleNamespace(
        transport="imap", imap=imap, smtp=smtp, mailbox="desk@example.org", gmail=SimpleNamespace(label_ids=["INBOX"])))


def response(status=200, data=None, *, content=None):
    kw = {"content": content} if content is not None else {"json": data or {}}
    return httpx.Response(status, request=httpx.Request("GET", "https://mail.example.test"), **kw)


@pytest.mark.parametrize("tls", ["implicit", "starttls"])
def test_imap_and_smtp_connect_use_tls_and_secret(conn, monkeypatch, tls):
    import imapclient
    monkeypatch.setenv("TEST_MAIL_PASSWORD", "test-only-password")
    conn.settings.imap.tls = conn.settings.smtp.tls = tls
    imap, smtp = MagicMock(), MagicMock()
    imap_factory, smtp_factory = MagicMock(return_value=imap), MagicMock(return_value=smtp)
    monkeypatch.setattr(imapclient, "IMAPClient", imap_factory)
    monkeypatch.setattr(smtplib, "SMTP_SSL" if tls == "implicit" else "SMTP", smtp_factory)
    assert t._imap_connect(conn) is imap
    assert t._smtp_connect(conn) is smtp
    assert imap_factory.call_args.kwargs["ssl"] is (tls == "implicit")
    assert imap.login.call_args.args == ("desk@example.org", "test-only-password")
    assert smtp.login.call_args.args == ("desk@example.org", "test-only-password")
    assert imap.starttls.call_count == smtp.starttls.call_count == int(tls == "starttls")


def test_missing_password_reports_reference_without_secret(conn, monkeypatch):
    monkeypatch.delenv("TEST_MAIL_PASSWORD", raising=False)
    with pytest.raises(ChannelError) as exc:
        t._password(conn, "imap")
    assert exc.value.code == "credential_missing" and exc.value.status == 503
    assert "env:TEST_MAIL_PASSWORD" in exc.value.message


def test_custom_ca_and_http_singleton(monkeypatch):
    ctx = MagicMock()
    monkeypatch.setattr(t.ssl, "create_default_context", lambda: ctx)
    monkeypatch.setenv("HUBZOID_EMAIL_CA_FILE", "/fake/ca.pem")
    assert t._tls_context() is ctx
    ctx.load_verify_locations.assert_called_once_with("/fake/ca.pem")
    fake = MagicMock()
    factory = MagicMock(return_value=fake)
    monkeypatch.setattr(t, "_client", None)
    monkeypatch.setattr(httpx, "Client", factory)
    assert t.http() is fake and t.http() is fake
    factory.assert_called_once_with(follow_redirects=False, timeout=30)
    checks = t._override_checks()
    assert checks[0].status == "warn" and "HUBZOID_EMAIL_CA_FILE" in checks[0].message


@pytest.mark.parametrize("error,kind,message", [
    (smtplib.SMTPAuthenticationError(535, b"no"), Rejected, "blocked_credentials"),
    (OSError("offline"), NotSent, "Could not reach"),
    (smtplib.SMTPConnectError(421, b"down"), NotSent, "Could not reach"),
])
def test_smtp_connect_failure_classified_before_send(conn, monkeypatch, error, kind, message):
    def connect(_):
        raise error
    monkeypatch.setattr(t, "_smtp_connect", connect)
    with pytest.raises(kind, match=message):
        t.ImapSmtp().send(conn, b"raw", to="person@example.org")


@pytest.mark.parametrize("error,kind,message", [
    (smtplib.SMTPRecipientsRefused({"person@example.org": (550, b"no")}), Rejected, "refused the message"),
    (smtplib.SMTPSenderRefused(550, b"no", "desk@example.org"), Rejected, "refused the message"),
    (smtplib.SMTPDataError(450, b"later"), NotSent, "deferred the message"),
    (smtplib.SMTPDataError(550, b"no"), Rejected, "refused the message"),
    (smtplib.SMTPServerDisconnected("lost"), smtplib.SMTPServerDisconnected, "lost"),
])
def test_smtp_send_failure_is_not_misreported_as_success(conn, monkeypatch, error, kind, message):
    smtp = MagicMock()
    smtp.sendmail.side_effect = error
    smtp.quit.side_effect = OSError("quit failed")
    monkeypatch.setattr(t, "_smtp_connect", lambda _: smtp)
    with pytest.raises(kind, match=message):
        t.ImapSmtp().send(conn, b"raw", to="person@example.org")
    smtp.sendmail.assert_called_once_with("desk@example.org", ["person@example.org"], b"raw")
    assert smtp.quit.call_count == 1


@pytest.mark.parametrize("folders,found,expected", [
    ([], [], None), ([(('\\Sent',), '/', 'Sent')], [], False),
    ([((b'\\Sent',), '/', 'Sent')], [9], "imap-sent:9"),
])
def test_imap_sent_lookup_distinguishes_unknown_absent_found(conn, monkeypatch, folders, found, expected):
    imap = MagicMock()
    imap.list_folders.return_value, imap.search.return_value = folders, found
    imap.logout.side_effect = OSError("logout failed")
    monkeypatch.setattr(t, "_imap_connect", lambda _: imap)
    assert t.ImapSmtp().find_sent(conn, "<message>") == expected
    assert imap.logout.call_count == 1


@pytest.mark.parametrize("folders,found,error", [([], [], "draft_unsupported"),
    ([((b'\\Drafts',), '/', 'Drafts')], [], "could not be read back")])
def test_imap_draft_without_folder_or_readback_refused(conn, monkeypatch, folders, found, error):
    imap = MagicMock()
    imap.list_folders.return_value, imap.search.return_value = folders, found
    monkeypatch.setattr(t, "_imap_connect", lambda _: imap)
    with pytest.raises((NotSent, RuntimeError), match=error):
        t.ImapSmtp().save_draft(conn, b"raw", message_id="<draft>")
    assert imap.logout.call_count == 1


def test_imap_oversize_and_deleted_messages_and_recovery_gap(conn, monkeypatch):
    box = Box()
    box.deliver(b"long")
    box.deliver(b"ok")
    imap = FakeImap(box)
    fetch = imap.fetch
    monkeypatch.setattr(imap, "fetch", lambda uids, fields: fetch([u for u in uids if u in box.folders["INBOX"]], fields))
    monkeypatch.setattr(t, "_imap_connect", lambda _: imap)
    monkeypatch.setattr(t, "MAX_RAW", 2)
    messages, cursor = t.ImapSmtp().fetch_new(conn, {"uidvalidity": box.validity, "last_uid": 0, "pending": [99]})
    assert [(m["raw"], m["too_large"]) for m in messages] == [(b"", True), (b"ok", False)]
    assert cursor["pending"] == [] and cursor["last_uid"] == 2
    monkeypatch.setattr(t, "RECOVERY_MAX", 1)
    messages, cursor = t.ImapSmtp().fetch_new(conn, {"uidvalidity": 999, "last_uid": 0, "since": "invalid"})
    assert cursor["gap"] == 1 and [m["raw"] for m in messages] == [b"ok"]
    assert t._date("2026-11-22").isoformat() == "2026-11-21"
    assert t._date(None) == datetime.now(timezone.utc).date() - timedelta(days=2)


def test_imap_doctor_does_not_send_and_reports_both_failures(conn, monkeypatch):
    def failed(_):
        raise OSError("offline")
    monkeypatch.setattr(t, "_imap_connect", failed)
    monkeypatch.setattr(t, "_smtp_connect", failed)
    checks = t.ImapSmtp().doctor(conn)
    assert [(c.id, c.status, c.code) for c in checks] == [
        ("email:imap", "fail", "credential_rejected"), ("email:smtp", "fail", "credential_rejected")]
    conn.settings.smtp = None
    checks = t.ImapSmtp().doctor(conn)
    assert checks[-1].code == "smtp_missing"
    monkeypatch.setattr(t, "_imap_connect", lambda _: (_ for _ in ()).throw(ChannelError("credential_missing", "Set it")))
    with pytest.raises(ChannelError, match="Set it"):
        t.ImapSmtp().doctor(conn)


@pytest.mark.parametrize("status,kind,message", [(429, NotSent, "slow down"), (401, Rejected, "blocked_credentials"),
    (403, Rejected, "blocked_credentials"), (422, Rejected, "refused the message"),
    (503, RuntimeError, "outcome is unknown")])
def test_mail_api_send_status_classification(conn, monkeypatch, status, kind, message):
    api = t.Gmail()
    monkeypatch.setattr(api, "call", lambda *a, **kw: response(status))
    with pytest.raises(kind, match=message):
        api.send(conn, b"raw", to="person@example.org")


@pytest.mark.parametrize("error,message", [(ChannelError("auth_required", "Reconnect"), "auth_required: Reconnect"),
                                            (httpx.ConnectError("offline"), "Could not reach")])
def test_mail_api_proven_unsent_errors(conn, monkeypatch, error, message):
    api = t.Gmail()
    monkeypatch.setattr(api, "call", lambda *a, **kw: (_ for _ in ()).throw(error))
    with pytest.raises(NotSent, match=message):
        api.send(conn, b"raw", to="person@example.org")


@pytest.mark.parametrize("method,sent", [("GET", False), ("POST", True)])
def test_api_transport_exception_marks_possible_send(conn, monkeypatch, method, sent):
    error = httpx.ReadTimeout("timed out")
    fake = MagicMock()
    fake.request.side_effect = error
    monkeypatch.setattr(t, "http", lambda: fake)
    monkeypatch.setattr(oauth, "access_token", lambda _: "test-token")
    with pytest.raises(httpx.ReadTimeout) as exc:
        t.Gmail().call(conn, method, "/resource")
    assert exc.value.hubzoid_sent is sent
    assert fake.request.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("cls", [t.Gmail, t.Graph])
@pytest.mark.parametrize("status,data,expected", [(503, {}, None), (200, {}, False),
    (200, {"messages": [{"id": "sent"}], "value": [{"id": "sent"}]}, "sent")])
def test_api_find_sent_has_three_distinct_outcomes(conn, monkeypatch, cls, status, data, expected):
    api = cls()
    calls = []
    def call(*args, **kw):
        calls.append((args, kw))
        return response(status, data)
    monkeypatch.setattr(api, "call", call)
    assert api.find_sent(conn, "<it's>") == expected
    if cls is t.Graph:
        assert calls[0][1]["params"]["$filter"] == "internetMessageId eq '<it''s>'"


@pytest.mark.parametrize("cls", [t.Gmail, t.Graph])
@pytest.mark.parametrize("status", [200, 403])
def test_api_identity_lookup(conn, monkeypatch, cls, status):
    api = cls()
    monkeypatch.setattr(api, "call", lambda *a, **kw: response(status, {"emailAddress": "desk@example.org",
                                                                      "userPrincipalName": "desk@example.org"}))
    if status == 200:
        assert api.whoami("token") == "desk@example.org"
    else:
        with pytest.raises(ChannelError) as exc:
            api.whoami("token")
        assert exc.value.code == "oauth_failed" and exc.value.status == 502


@pytest.mark.parametrize("cls", [t.Gmail, t.Graph])
@pytest.mark.parametrize("token,status,who,expected", [(None, 200, "desk@example.org", "auth_required"),
    ("token", 401, "desk@example.org", "credential_rejected"), ("token", 200, "other@example.org", "mailbox_mismatch"),
    ("token", 200, "DESK@example.org", "")])
def test_api_doctor_checks_current_token_and_mailbox_without_refresh(conn, monkeypatch, cls, token, status, who, expected):
    monkeypatch.setattr(oauth, "current_token", lambda _: token)
    monkeypatch.setattr(oauth, "access_token", lambda _: pytest.fail("doctor must not refresh"))
    api = cls()
    monkeypatch.setattr(api, "call", lambda *a, **kw: response(status, {"mail": who, "emailAddress": who}))
    check = api.doctor(conn)[-1]
    assert check.code == expected
    assert check.status == ("ok" if not expected else "warn" if token is None else "fail")


@pytest.mark.parametrize("cls", [t.Gmail, t.Graph])
def test_api_draft_must_be_read_back(conn, monkeypatch, cls):
    api = cls()
    monkeypatch.setattr(api, "send_call", lambda *a, **kw: response(200, {"id": "d1"}))
    monkeypatch.setattr(api, "call", lambda *a, **kw: response(404))
    with pytest.raises(RuntimeError, match="could not be read back"):
        api.save_draft(conn, b"raw", message_id="<draft>", thread_id="t1")


def test_gmail_expired_history_recovers_pages_and_reports_gap(conn, monkeypatch):
    api = t.Gmail()
    monkeypatch.setattr(t, "RECOVERY_MAX", 2)
    calls = []
    answers = iter([response(404), response(200, {"messages": [{"id": "3"}, {"id": "2"}], "nextPageToken": "p2"}),
                    response(200, {"messages": [{"id": "1"}]}), response(200, {"historyId": "fresh"}),
                    response(404), response(200, {"raw": t._b64url(b"raw"), "threadId": "t3"})])
    def call(*args, **kw):
        calls.append((args, kw))
        return next(answers)
    monkeypatch.setattr(api, "call", call)
    messages, cursor = api.fetch_new(conn, {"history_id": "old", "pending": []})
    assert cursor == {"history_id": "fresh", "pending": [], "gap": 1}
    assert messages == [{"id": "gmail:3", "raw": b"raw", "too_large": False, "thread_id": "t3"}]
    assert calls[2][1]["params"]["pageToken"] == "p2"


def test_gmail_incomplete_history_keeps_cursor_and_oversize_mail_is_not_loaded(conn, monkeypatch):
    api = t.Gmail()
    monkeypatch.setattr(t, "MAX_RAW", 2)
    def call(_, method, path, **kw):
        if path.endswith("/history"):
            return response(200, {"historyId": "new", "nextPageToken": "more", "history": [
                {"messagesAdded": [{"message": {"id": "1", "labelIds": ["INBOX"]}},
                                   {"message": {"id": "other", "labelIds": ["OTHER"]}}]}]})
        return response(200, {"raw": t._b64url(b"long"), "threadId": "t1"})
    monkeypatch.setattr(api, "call", call)
    messages, cursor = api.fetch_new(conn, {"history_id": "old", "pending": []})
    assert cursor["history_id"] == "old"
    assert messages == [{"id": "gmail:1", "raw": b"", "too_large": True, "thread_id": "t1"}]


def test_graph_initial_delta_must_finish(conn, monkeypatch):
    api = t.Graph()
    monkeypatch.setattr(api, "_get_url", lambda *a: response(200, {"@odata.nextLink": "https://graph.example.test/more"}))
    with pytest.raises(ChannelError) as exc:
        api.fetch_new(conn, None)
    assert exc.value.code == "provider_unavailable" and exc.value.status == 503


def test_graph_expired_delta_recovers_mail_and_new_cursor(conn, monkeypatch):
    api = t.Graph()
    monkeypatch.setattr(t, "RECOVERY_MAX", 1)
    answers = iter([response(410), response(200, {"value": [{"id": "a"}, {"id": "b"}]}),
                    response(200, {"@odata.deltaLink": "fresh"})])
    monkeypatch.setattr(api, "_get_url", lambda *a: next(answers))
    replies = iter([response(200, content=b"raw"), response(503)])
    monkeypatch.setattr(api, "call", lambda *a, **kw: next(replies))
    messages, cursor = api.fetch_new(conn, {"delta_link": "old", "since": "2026-11-01", "pending": []})
    assert cursor["delta_link"] == "fresh" and cursor["gap"] == 1
    assert messages == [{"id": "graph:a", "raw": b"raw", "too_large": False, "thread_id": None}]


def test_graph_delta_ignores_deletions_follows_pages_and_keeps_pending(conn, monkeypatch):
    api = t.Graph()
    answers = iter([response(200, {"value": [{"id": "deleted", "@removed": {}}], "@odata.nextLink": "more"}),
                    response(200, {"value": [{"id": "a"}, {"id": "b"}], "@odata.deltaLink": "new"})])
    monkeypatch.setattr(api, "_get_url", lambda *a: next(answers))
    monkeypatch.setattr(t, "BATCH", 1)
    monkeypatch.setattr(api, "call", lambda *a, **kw: response(404))
    messages, cursor = api.fetch_new(conn, {"delta_link": "old", "pending": []})
    assert messages == [] and cursor["pending"] == ["b"] and cursor["delta_link"] == "new"
