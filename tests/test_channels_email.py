"""The email channel and the intake service with a fake mailbox: the sender
gate (allowlist and aligned DKIM), new-only polling, deduplication after a
UIDVALIDITY change, quarantine and release, attachments, and the three write
modes for replies. No network and no model."""
from __future__ import annotations

import base64
import json
import textwrap
from email.message import EmailMessage

import dkim
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hubzoid.channels import config, store
from hubzoid.channels.email import mime, oauth, poller, transports
from hubzoid.channels.errors import ChannelError

MAILBOX = "orders@desk.example"
CUSTOMER = "buyer@shop.example"
PASSWORD = "imap-test-value"     # gitleaks:allow (a test value)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                         serialization.NoEncryption())
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.DER,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def dns(name, timeout=5):
    if name == b"sel._domainkey.shop.example.":
        return f"v=DKIM1; k=rsa; p={PUB}".encode()
    return b""


def message(*, sender=CUSTOMER, subject="Order for 40 units", body="Please send 40 units of SKU-7.", msgid=None,
            sign=True, attachments=(), extra=None, length=False) -> bytes:
    m = EmailMessage()
    m["From"] = f"Buyer <{sender}>"
    m["To"] = MAILBOX
    m["Subject"] = subject
    m["Message-ID"] = msgid or f"<{abs(hash((subject, body)))}@shop.example>"
    m["Date"] = "Mon, 19 Oct 2026 10:00:00 +0000"
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    for name, ctype, data in attachments:
        main, sub = ctype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    raw = bytes(m)
    if sign:
        domain = sender.rpartition("@")[2].encode()
        sig = dkim.sign(raw, b"sel", domain, PRIV, include_headers=[b"from", b"subject", b"to", b"date"],
                        length=length)
        raw = sig + raw
    return raw


class Box:
    """One fake IMAP account: folders of {uid: raw}."""

    def __init__(self):
        self.validity = 1
        self.folders = {"INBOX": {}, "Drafts": {}, "Sent": {}}
        self.sent: list[tuple[str, list, bytes]] = []
        self.smtp_fail = None

    def deliver(self, raw, folder="INBOX"):
        f = self.folders[folder]
        f[max(f, default=0) + 1] = raw


class FakeImap:
    def __init__(self, box):
        self.box, self.folder = box, None

    def select_folder(self, folder, readonly=False):
        assert readonly, "the poller never opens a folder read-write"
        self.folder = folder
        f = self.box.folders[folder]
        return {b"UIDVALIDITY": self.box.validity, b"UIDNEXT": max(f, default=0) + 1}

    def search(self, criteria):
        f = self.box.folders[self.folder]
        if criteria[0] == "UID":
            lo = int(criteria[1].split(":")[0])
            hits = [u for u in f if u >= lo]
            return hits or ([max(f)] if f else [])      # IMAP's n:* returns the last message
        if criteria[0] == "SINCE":
            return list(f)
        if criteria[0] == "HEADER":
            return [u for u, raw in f.items() if criteria[2].encode() in raw]
        raise AssertionError(criteria)

    def fetch(self, uids, items):
        f = self.box.folders[self.folder]
        if items == ["RFC822.SIZE"]:
            return {u: {b"RFC822.SIZE": len(f[u])} for u in uids}
        assert items == ["BODY.PEEK[]"]
        return {u: {b"BODY[]": f[u]} for u in uids}

    def append(self, folder, raw, flags=()):
        self.box.deliver(raw, folder)

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"/", "INBOX"), ((b"\\Drafts",), b"/", "Drafts"),
                ((b"\\Sent",), b"/", "Sent")]

    def logout(self):
        pass


class FakeSmtp:
    def __init__(self, box):
        self.box = box

    def sendmail(self, frm, to, raw):
        if self.box.smtp_fail is not None:
            exc, self.box.smtp_fail = self.box.smtp_fail, None
            raise exc
        self.box.sent.append((frm, to, raw))
        self.box.deliver(raw, "Sent")

    def noop(self):
        return (250, b"ok")

    def quit(self):
        pass


@pytest.fixture
def box(monkeypatch):
    b = Box()
    monkeypatch.setattr(transports, "_imap_connect", lambda conn: FakeImap(b))
    monkeypatch.setattr(transports, "_smtp_connect", lambda conn: FakeSmtp(b))
    monkeypatch.setattr(mime, "verify_dkim", lambda raw, sender, dnsfunc=None, _real=mime.verify_dkim:
                        _real(raw, sender, dnsfunc=dnsfunc or dns))
    return b


@pytest.fixture
def started(monkeypatch):
    """Work items record but do not run (no DBOS). The approver's account
    check is milestone D's, tested there."""
    from hubzoid.work_items import operations
    from hubzoid.work_items import service as wi

    monkeypatch.setattr(wi, "start", lambda hub_dir, item: item)
    monkeypatch.setattr(operations, "_approver_still_allowed", lambda hub_dir, hub, who: bool(who))
    monkeypatch.setattr(mime, "verify_dkim", lambda raw, sender, dnsfunc=None, _real=mime.verify_dkim:
                        _real(raw, sender, dnsfunc=dnsfunc or dns))


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_PUBLIC_URL", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMAIL_TEST_PASSWORD", PASSWORD)
    root = tmp_path / "desk"
    (root / "workflows" / "orders").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nThe order desk.\n")
    (root / "workflows" / "orders" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow
        from hubzoid.work_items import Route

        @workflow(work_items={"intake": Route(actions=("email.send",))})
        def orders():
            return {"ok": True}
    '''))
    return root


def imap_conn(hub_dir, **pc):
    tls = {"port": 993, "tls": "implicit", "username": MAILBOX, "password_ref": "env:EMAIL_TEST_PASSWORD"}
    provider_config = {"transport": "imap", "mailbox": MAILBOX, "imap": {"host": "imap.desk.example", **tls},
                       "smtp": {"host": "smtp.desk.example", **tls, "port": 465},
                       "sender_policy": {"allowed": [CUSTOMER]}, **pc}
    v = config.load_text(hub_dir, config.render({"version": 1, "id": "orders-email", "provider": "email",
                                                 "enabled": True, "workflow": "orders", "run_as": "workflow",
                                                 "provider_config": provider_config}))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    return config.active(hub_dir, "orders-email")


def route_all(hub_dir):
    from hubzoid.channels import router

    out = []
    for row in store.open_deliveries(hub_dir, "desk"):
        out.append(router._route_one(hub_dir, row, None))
    return out


def items(hub_dir, **kw):
    from hubzoid.intake import service

    return service.list_items(hub_dir, **kw)["items"]


# -- configuration ---------------------------------------------------------------------

def test_config_rules(hub_dir):
    base = {"version": 1, "id": "e", "provider": "email", "workflow": "orders", "run_as": "workflow"}
    v = config.load_text(hub_dir, config.render({**base, "provider_config": {"transport": "gmail",
                                                                             "mailbox": MAILBOX}}))
    assert not v.valid and "needs its gmail settings" in v.errors[0]["message"]
    v = config.load_text(hub_dir, config.render({**base, "provider_config": {
        "transport": "imap", "mailbox": MAILBOX, "imap": {"host": "h", "port": 993, "tls": "implicit",
                                                          "username": "u", "password_ref": "env:X"},
        "sender_policy": {"allowed": ["*@shop.example"]}}}))
    assert not v.valid and "no wildcards" in json.dumps(v.errors)
    v = config.load_text(hub_dir, config.render({**base, "workflow": None, "agent": "main", "run_as": "linked_requester",
                                                 "allowed_groups": ["ops"], "provider_config": {
                                                     "transport": "imap", "mailbox": MAILBOX, "imap": {
                                                         "host": "h", "port": 993, "tls": "implicit", "username": "u",
                                                         "password_ref": "env:X"}}}))
    fields = {e["field"] for e in v.errors}
    assert {"/agent", "/run_as", "/allowed_groups"} <= fields


# -- reading mail safely -----------------------------------------------------------------

def test_html_only_mail_is_inert_text():
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = CUSTOMER, "Hi", "<h1@shop.example>"
    m.set_content("<html><head><style>x{}</style></head><body><p>Order <b>40</b></p><script>alert(1)</script>"
                  "<img src='https://tracker.example/p.gif'></body></html>", subtype="html")
    p = mime.parse(bytes(m))
    assert p.text == "Order 40" and "tracker" not in p.text and "alert" not in p.text


def test_two_from_addresses_are_quarantined():
    raw = message(sign=False).replace(f"From: Buyer <{CUSTOMER}>".encode(),
                                      f"From: Buyer <{CUSTOMER}>, boss@desk.example".encode())
    p = mime.parse(raw)
    trust = mime.sender_trust(raw, p, mailbox=MAILBOX, allowed=[CUSTOMER], authentication="aligned_dkim")
    assert trust == {"decision": "quarantined", "reason": "from_not_single", "evidence": {}}


@pytest.mark.parametrize("raw,decision,reason", [
    (lambda: message(), "accepted", "aligned_dkim"),
    (lambda: message(sign=False), "quarantined", "sender_unverified"),
    (lambda: message(sender="stranger@else.example"), "quarantined", "unknown_sender"),
    (lambda: message(length=True), "quarantined", "sender_unverified"),
    (lambda: message(sender=MAILBOX), "rejected", "own_mailbox"),
    (lambda: message(extra={"Auto-Submitted": "auto-replied"}), "rejected", "automatic_message"),
])
def test_sender_gate(raw, decision, reason):
    raw = raw()
    allowed = [CUSTOMER, "stranger@nowhere.example"]
    trust = mime.sender_trust(raw, mime.parse(raw), mailbox=MAILBOX, allowed=allowed, authentication="aligned_dkim",
                              dnsfunc=dns)
    assert (trust["decision"], trust["reason"]) == (decision, reason)


def test_signed_by_another_domain_is_not_aligned():
    raw = message(sign=False)
    raw = dkim.sign(raw, b"sel", b"other.example", PRIV, include_headers=[b"from", b"subject"]) + raw
    ok, ev = mime.verify_dkim(raw, CUSTOMER, dnsfunc=dns)
    assert not ok and "sender's domain" in ev["detail"]


def test_a_changed_body_breaks_the_signature():
    raw = message().replace(b"40 units of SKU-7", b"400 units of SKU-7")
    ok, ev = mime.verify_dkim(raw, CUSTOMER, dnsfunc=dns)
    assert not ok and ev["reason"] == "sender_unverified"


def test_reply_goes_only_to_the_sender_in_the_same_conversation():
    raw = mime.build_reply(mailbox=MAILBOX, to=CUSTOMER, subject="Order", body="Confirmed.",
                           in_reply_to="<b@shop.example>", references=["<a@shop.example>"], message_id="<r@desk.example>")
    m = mime.parse(raw)
    assert m.subject == "Re: Order" and m.in_reply_to == "<b@shop.example>"
    assert m.references == ["<a@shop.example>", "<b@shop.example>"]
    text = raw.decode()
    assert "Cc:" not in text and "Bcc:" not in text and f"To: {CUSTOMER}" in text


# -- polling -------------------------------------------------------------------------------

def test_first_poll_reads_nothing_old(hub_dir, box, started):
    box.deliver(message(subject="old order"))
    conn = imap_conn(hub_dir)
    out = poller.poll_once(conn)
    assert out == {"state": "started", "messages": 0, "deliveries": []}
    assert store.list_deliveries(hub_dir, "desk", conn.id)["items"] == []


def test_trusted_sender_starts_work_as_the_workflow(hub_dir, box, started):
    from hubzoid.work_items import store as wstore

    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(attachments=[("po.pdf", "application/pdf", b"%PDF-1.4 order")]))
    out = poller.poll_once(conn)
    assert out["messages"] == 1 and out["deliveries"][0]["trust"] == "accepted"
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "dispatched" and item["sender"]["external_id"] == CUSTOMER
    work = wstore.get_item(hub_dir, item["work_item_id"])
    assert work["requester"] == "workflow:orders"
    assert work["origin_json"]["requested_by"]["external_id"] == CUSTOMER
    payload = work["input_json"].get("input", work["input_json"])
    assert payload["attachments"][0]["filename"] == "po.pdf" and payload["sender"]["external_id"] == CUSTOMER
    assert "untrusted" in payload["note"]
    from hubzoid.intake import service

    info, data = service.attachment_bytes(hub_dir, payload["attachments"][0]["attachment_id"])
    assert data == b"%PDF-1.4 order" and info["sha256"].startswith("sha256:")


def test_unknown_sender_waits_and_nothing_is_sent(hub_dir, box, started):
    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(sender="stranger@else.example"))
    poller.poll_once(conn)
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "quarantined" and item["reason"] == "unknown_sender" and not item["work_item_id"]
    assert box.sent == []


def test_own_and_automatic_mail_never_becomes_work(hub_dir, box, started):
    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(sender=MAILBOX))
    box.deliver(message(subject="Out of office", extra={"Auto-Submitted": "auto-replied"}))
    poller.poll_once(conn)
    route_all(hub_dir)
    reasons = {r["reason"] for r in store.list_deliveries(hub_dir, "desk", conn.id)["items"]}
    assert reasons == {"own_mailbox", "automatic_message"} and items(hub_dir) == []


def test_uidvalidity_change_does_not_duplicate(hub_dir, box, started):
    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message())
    poller.poll_once(conn)
    box.validity = 2                         # the server renumbered the folder
    out = poller.poll_once(conn)
    assert out["deliveries"][0]["status"] == "duplicate"
    route_all(hub_dir)
    assert len(items(hub_dir)) == 1


def test_disallowed_attachment_holds_the_whole_message(hub_dir, box, started):
    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(attachments=[("tool.exe", "application/x-msdownload", b"MZ...")]))
    poller.poll_once(conn)
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "failed" and item["reason"] == "attachment_failed" and not item["work_item_id"]


# -- people's decisions ----------------------------------------------------------------------

def _grant(hub_dir, email, *perms):
    from hubzoid.access import store_for

    for perm in perms:
        store_for(hub_dir).grant(email, "desk", perm, actor="test")


def test_release_starts_attended_work_and_keeps_the_sender(hub_dir, box, started):
    from hubzoid.intake import service
    from hubzoid.work_items import store as wstore

    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(sign=False))
    poller.poll_once(conn)
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["reason"] == "sender_unverified"
    with pytest.raises(ChannelError) as exc:
        service.release(hub_dir, item["id"], actor="ops@desk.example", reason="known buyer",
                        expected_revision=item["revision"])
    assert exc.value.status == 403                       # no grant to run the workflow
    assert service.require(hub_dir, item["id"])["state"] == "quarantined"
    _grant(hub_dir, "ops@desk.example", "workflows_manage")
    with pytest.raises(ChannelError):
        service.release(hub_dir, item["id"], actor="ops@desk.example", reason="", expected_revision=item["revision"])
    cur = service.require(hub_dir, item["id"])
    out = service.release(hub_dir, item["id"], actor="ops@desk.example", reason="known buyer",
                          expected_revision=cur["revision"])
    work = wstore.get_item(hub_dir, out["work_item_id"])
    assert out["state"] == "dispatched" and work["requester"] == "ops@desk.example"
    assert work["origin_json"]["requested_by"]["external_id"] == CUSTOMER
    assert work["origin_json"]["acts_as"] == "workflow:orders"


def test_reject_closes_without_sending(hub_dir, box, started):
    from hubzoid.intake import service

    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(sender="stranger@else.example"))
    poller.poll_once(conn)
    route_all(hub_dir)
    [item] = items(hub_dir)
    out = service.reject(hub_dir, item["id"], actor="ops@desk.example", reason="spam",
                         expected_revision=item["revision"])
    assert out["state"] == "rejected" and box.sent == []
    with pytest.raises(ChannelError) as exc:
        service.release(hub_dir, item["id"], actor="ops@desk.example", reason="x", expected_revision=out["revision"])
    assert exc.value.code == "invalid_state"


# -- replies ---------------------------------------------------------------------------------

def _released(hub_dir, box):
    from hubzoid.intake import service
    from hubzoid.work_items import store as wstore

    conn = imap_conn(hub_dir)
    poller.poll_once(conn)
    box.deliver(message(sign=False, msgid="<order-1@shop.example>"))
    poller.poll_once(conn)
    route_all(hub_dir)
    [item] = items(hub_dir)
    _grant(hub_dir, "ops@desk.example", "workflows_manage")
    out = service.release(hub_dir, item["id"], actor="ops@desk.example", reason="known buyer",
                          expected_revision=item["revision"])
    return conn, wstore.get_item(hub_dir, out["work_item_id"])


def _post(hub_dir, work, text="Confirmed: 40 units ship Friday."):
    from hubzoid import db
    from hubzoid.channels import thread
    from hubzoid.workflows import context

    with context.run_scope(hub="desk", workflow="orders", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           run_id="run-1", work={"item": work, "stage": None}):
        return thread.for_current_run().post(text, key="answer", kind="result")


def _mode(hub_dir, mode):
    (hub_dir / "work-items.yaml").write_text(f"version: 1\nwrite_modes:\n  email.send: {mode}\n")


def test_off_sends_nothing(hub_dir, box, started):
    from hubzoid.work_items.errors import WorkItemError

    conn, work = _released(hub_dir, box)
    with pytest.raises(WorkItemError) as exc:
        _post(hub_dir, work)
    assert exc.value.code in ("action_off", "write_off", "off") or "off" in exc.value.message
    assert box.sent == []


def test_draft_needs_a_rehearsed_mailbox_then_saves_a_real_draft(hub_dir, box, started):
    from hubzoid.channels.email import status

    conn, work = _released(hub_dir, box)
    _mode(hub_dir, "draft")
    with pytest.raises(Exception) as exc:
        _post(hub_dir, work)
    assert "draft_unsupported" in str(exc.value)
    out = status.rehearse_draft(config.active(hub_dir, conn.id), actor="ops@desk.example")
    assert out["draft_verified"] and box.sent == []
    _post(hub_dir, work)
    drafts = list(box.folders["Drafts"].values())
    assert len(drafts) == 2 and b"In-Reply-To: <order-1@shop.example>" in drafts[-1] and box.sent == []


def test_approve_waits_for_the_requester_then_sends_once(hub_dir, box, started):
    from hubzoid.work_items import service as wi
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked, WorkItemError

    conn, work = _released(hub_dir, box)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    assert op["state"] == "awaiting_approval" and box.sent == []
    with pytest.raises(WorkItemError) as exc:
        wi.approve(hub_dir, op["id"], "someone@desk.example", digest=op["args_digest"])
    assert exc.value.code == "approver_not_allowed"
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    _post(hub_dir, work)
    _post(hub_dir, work)                                     # the same post again sends nothing new
    assert len(box.sent) == 1
    frm, to, raw = box.sent[0]
    assert to == [CUSTOMER] and b"In-Reply-To: <order-1@shop.example>" in raw


def test_smtp_drop_after_data_stays_uncertain(hub_dir, box, started):
    import smtplib

    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked

    conn, work = _released(hub_dir, box)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    box.smtp_fail = smtplib.SMTPServerDisconnected("lost after DATA")
    with pytest.raises(Parked) as again:
        _post(hub_dir, work)
    assert wstore.get_op(hub_dir, again.value.operation_id)["state"] == "ambiguous"
    _post_after = box.sent
    assert _post_after == []


# -- Gmail and Microsoft 365 -------------------------------------------------------------------

class FakeGoogle:
    def __init__(self):
        self.history = []
        self.messages = {}
        self.sent = []
        self.drafts = {}
        self.profile_email = MAILBOX

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "ya29.test", "refresh_token": "1//test",  # gitleaks:allow
                                             "expires_in": 3600, "scope": "gmail.readonly gmail.send"})
        if path == "/gmail/v1/users/me/profile":
            return httpx.Response(200, json={"emailAddress": self.profile_email, "historyId": str(100 + len(self.history))})
        if path == "/gmail/v1/users/me/history":
            start = int(request.url.params["startHistoryId"])
            hist = [h for n, h in enumerate(self.history, 101) if n > start]
            return httpx.Response(200, json={"history": hist, "historyId": str(100 + len(self.history))})
        if path.startswith("/gmail/v1/users/me/messages/") and request.method == "GET":
            mid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": mid, "threadId": "t-" + mid,
                                             "raw": base64.urlsafe_b64encode(self.messages[mid]).decode()})
        if path == "/gmail/v1/users/me/messages/send":
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}"})
        if path == "/gmail/v1/users/me/messages":
            q = request.url.params.get("q", "")
            hits = [{"id": f"sent-{n}"} for n, s in enumerate(self.sent, 1)
                    if q.replace("rfc822msgid:", "").encode() in base64.urlsafe_b64decode(s["raw"] + "==")]
            return httpx.Response(200, json={"messages": hits})
        return httpx.Response(404, json={"error": {"message": f"not faked {path}"}})

    def arrive(self, raw):
        mid = f"m{len(self.messages) + 1}"
        self.messages[mid] = raw
        self.history.append({"messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}]})


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.test")
    monkeypatch.setenv("GMAIL_TEST_CLIENT", "client-secret-value")   # gitleaks:allow
    return fake


def gmail_conn(hub_dir):
    pc = {"transport": "gmail", "mailbox": MAILBOX, "sender_policy": {"allowed": [CUSTOMER]},
          "gmail": {"client_id": "client.apps.googleusercontent.com", "client_secret_ref": "env:GMAIL_TEST_CLIENT",
                    "oauth_ref": "secret:channel/desk/orders-email/gmail-oauth"}}
    v = config.load_text(hub_dir, config.render({"version": 1, "id": "orders-email", "provider": "email",
                                                 "enabled": True, "workflow": "orders", "run_as": "workflow",
                                                 "provider_config": pc}))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    return config.active(hub_dir, "orders-email")


def _consent(hub_dir, conn):
    from urllib.parse import parse_qs, urlparse

    out = oauth.connect(conn, actor="ops@desk.example", mode="approve")
    q = parse_qs(urlparse(out["authorization_url"]).query)
    assert "https://www.googleapis.com/auth/gmail.send" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    return oauth.callback(config.active(hub_dir, conn.id), code="code-1", state=q["state"][0])


def test_gmail_consent_checks_the_mailbox_and_keeps_tokens_secret(hub_dir, google, started):
    conn = gmail_conn(hub_dir)
    google.profile_email = "someone-else@desk.example"
    with pytest.raises(ChannelError) as exc:
        _consent(hub_dir, conn)
    assert exc.value.code == "mailbox_mismatch"
    google.profile_email = MAILBOX
    out = _consent(hub_dir, config.active(hub_dir, conn.id))
    assert out["state"] == "connected"
    assert "ya29" not in json.dumps(config.export(hub_dir, conn.id), default=str)
    assert "ya29" not in json.dumps(config.active(hub_dir, conn.id).state, default=str)


def test_gmail_reads_new_mail_and_finds_a_sent_reply(hub_dir, google, started):
    conn = gmail_conn(hub_dir)
    _consent(hub_dir, conn)
    google.arrive(message(subject="before start"))
    assert poller.poll_once(conn)["state"] == "started"
    google.arrive(message(msgid="<g-1@shop.example>"))
    out = poller.poll_once(conn)
    assert out["messages"] == 1 and out["deliveries"][0]["trust"] == "accepted"
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "dispatched"
    conn = config.active(hub_dir, conn.id)
    t = transports.for_conn(conn)
    raw = mime.build_reply(mailbox=MAILBOX, to=CUSTOMER, subject="x", body="y", in_reply_to="<g-1@shop.example>",
                           references=[], message_id="<reply-1@desk.example>")
    t.send(conn, raw, to=CUSTOMER, thread_id="t-m2")
    assert google.sent[0]["threadId"] == "t-m2"
    assert t.find_sent(conn, "<reply-1@desk.example>") == "sent-1"
    assert t.find_sent(conn, "<never@desk.example>") is False


def test_consent_link_is_single_use(hub_dir, google, started):
    from urllib.parse import parse_qs, urlparse

    conn = gmail_conn(hub_dir)
    out = oauth.connect(conn, actor="ops@desk.example")
    state = parse_qs(urlparse(out["authorization_url"]).query)["state"][0]
    oauth.callback(config.active(hub_dir, conn.id), code="c", state=state)
    with pytest.raises(ChannelError) as exc:
        oauth.callback(config.active(hub_dir, conn.id), code="c", state=state)
    assert exc.value.code == "oauth_state_invalid"


class FakeGraph:
    def __init__(self):
        self.messages: dict[str, bytes] = {}
        self.delta_from = 0
        self.sent: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "eyJ.test", "refresh_token": "M.test",  # gitleaks:allow
                                             "expires_in": 3600, "scope": "Mail.ReadWrite Mail.Send"})
        if path.endswith("/me") and request.method == "GET":
            return httpx.Response(200, json={"mail": MAILBOX, "userPrincipalName": MAILBOX})
        if path.endswith("/messages/delta"):
            new = list(self.messages)[self.delta_from:] if "deltatoken" in str(request.url) else []
            self.delta_from = len(self.messages)
            return httpx.Response(200, json={"value": [{"id": m} for m in new],
                                             "@odata.deltaLink": "https://graph.test/v1.0/me/mailFolders/inbox/"
                                                                 f"messages/delta?$deltatoken={len(self.messages)}"})
        if path.endswith("/$value"):
            return httpx.Response(200, content=self.messages[path.split("/")[-2]])
        if "/me/messages/" in path and request.method == "GET":
            return httpx.Response(200, json={"conversationId": "conv-1"})
        if path.endswith("/me/sendMail"):
            self.sent.append(base64.b64decode(request.content))
            return httpx.Response(202)
        if path.endswith("/sentitems/messages"):
            f = request.url.params["$filter"]
            hit = [{"id": "s1"} for raw in self.sent if f.split("'")[1].encode() in raw]
            return httpx.Response(200, json={"value": hit})
        return httpx.Response(404, json={"error": {"code": "NotFound"}})


def test_graph_reads_by_delta_and_replies_in_the_conversation(hub_dir, monkeypatch, started):
    from urllib.parse import parse_qs, urlparse

    fake = FakeGraph()
    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.test")
    monkeypatch.setenv("HUBZOID_GRAPH_API_URL", "https://graph.test/v1.0")
    monkeypatch.setenv("M365_TEST_SECRET", "m365-secret-value")   # gitleaks:allow
    pc = {"transport": "microsoft365", "mailbox": MAILBOX, "sender_policy": {"allowed": [CUSTOMER]},
          "microsoft365": {"app_id": "00000000-0000-4000-8000-000000000001", "tenant_id": "contoso.example",
                           "client_secret_ref": "env:M365_TEST_SECRET",
                           "oauth_ref": "secret:channel/desk/orders-email/m365-oauth"}}
    v = config.load_text(hub_dir, config.render({"version": 1, "id": "orders-email", "provider": "email",
                                                 "enabled": True, "workflow": "orders", "run_as": "workflow",
                                                 "provider_config": pc}))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    conn = config.active(hub_dir, "orders-email")
    out = oauth.connect(conn, actor="ops@desk.example")
    assert "/contoso.example/oauth2/v2.0/authorize" in out["authorization_url"]
    state = parse_qs(urlparse(out["authorization_url"]).query)["state"][0]
    oauth.callback(config.active(hub_dir, conn.id), code="c", state=state)
    fake.messages["old"] = message(subject="old")
    assert poller.poll_once(conn)["state"] == "started"
    fake.messages["n1"] = message(msgid="<m-1@shop.example>")
    out = poller.poll_once(conn)
    assert out["messages"] == 1 and out["deliveries"][0]["trust"] == "accepted"
    conn = config.active(hub_dir, conn.id)
    t = transports.for_conn(conn)
    raw = mime.build_reply(mailbox=MAILBOX, to=CUSTOMER, subject="x", body="y", in_reply_to="<m-1@shop.example>",
                           references=[], message_id="<r-9@desk.example>")
    t.send(conn, raw, to=CUSTOMER)
    assert t.find_sent(conn, "<r-9@desk.example>") == "s1" and t.find_sent(conn, "<none@desk.example>") is False
