"""Owner email: one fixed recipient, explicit refusals, an honest preview mode,
and retry rules that never resend a message that may already have gone.

A tiny SMTP server on 127.0.0.1 exercises the real smtplib path over TCP; a
scripted fake covers STARTTLS/login failures. Nothing leaves the machine.
"""
from __future__ import annotations

from pathlib import Path

import logging
import smtplib
import socketserver
import threading

import pytest
from sqlalchemy import text

from hubzoid import artifacts as arts
from hubzoid import email_delivery as mail
from hubzoid.access import store_for
from hubzoid.workflows.identity import RunIdentity

OWNER = "priya@company.com"
ME = RunIdentity(OWNER, "id-priya", "run_as", OWNER)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSMTP:
    """Speaks just enough SMTP. `mail_codes` are answered to MAIL FROM, one per
    connection (then 250); `rcpt_code` to RCPT; `data` is ok | drop | reject."""

    def __init__(self, mail_codes=(), rcpt_code=250, data="ok"):
        self.mail_codes = list(mail_codes)
        self.rcpt_code = rcpt_code
        self.data = data
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []
        self.connections = 0
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                outer.connections += 1
                send = lambda s: self.wfile.write(s.encode() + b"\r\n")
                send("220 fake ESMTP")
                sender, rcpts = "", []
                while True:
                    line = self.rfile.readline().decode(errors="replace").strip()
                    if not line:
                        return
                    verb = line.split(" ", 1)[0].split(":", 1)[0].upper()
                    if verb in ("EHLO", "HELO"):
                        send("250-fake\r\n250 SIZE 1000000")
                    elif verb == "MAIL":
                        code = outer.mail_codes.pop(0) if outer.mail_codes else 250
                        sender = line
                        send(f"{code} mail")
                    elif verb == "RCPT":
                        rcpts.append(line.split(":", 1)[1].strip(" <>"))
                        send(f"{outer.rcpt_code} rcpt")
                    elif verb == "DATA":
                        send("354 go ahead")
                        body = b""
                        while True:
                            chunk = self.rfile.readline()
                            if chunk in (b".\r\n", b""):
                                break
                            body += chunk
                        if outer.data == "drop":
                            return                      # vanish before replying
                        if outer.data == "reject":
                            send("554 rejected")
                            continue
                        outer.messages.append(body)
                        outer.envelopes.append((sender, rcpts))
                        send("250 queued")
                    elif verb == "QUIT":
                        send("221 bye")
                        return
                    else:
                        send("250 ok")

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("WEBUI_AUTH", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_SMTP_HOST",
              "HUBZOID_SMTP_USERNAME", "HUBZOID_SMTP_PASSWORD", "HUBZOID_SMTP_SSL",
              "HUBZOID_EMAIL_DELIVERY", "HUBZOID_PUBLIC_URL", "WEBUI_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: sales\n---\nbody")
    store_for(d).upsert_identity(email=OWNER, owui_id="id-priya")
    return d


@pytest.fixture
def smtp(monkeypatch):
    servers = []

    def make(**kw):
        s = FakeSMTP(**kw)
        servers.append(s)
        monkeypatch.setenv("HUBZOID_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("HUBZOID_SMTP_PORT", str(s.port))
        monkeypatch.setenv("HUBZOID_SMTP_FROM", "hubzoid@company.com")
        monkeypatch.setenv("HUBZOID_SMTP_STARTTLS", "false")
        monkeypatch.setenv("HUBZOID_SMTP_TIMEOUT", "5")
        return s

    yield make
    for s in servers:
        s.close()


def _send(hub, **kw):
    kw.setdefault("sleep", lambda s: None)
    return mail.send_to_owner(hub, hub="sales", identity=kw.pop("identity", ME),
                              subject=kw.pop("subject", "Your Q3 report"),
                              body=kw.pop("body", "It is ready."), **kw)


def _rows(hub):
    with store_for(hub)._engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_email_deliveries"))]


# --- who receives it -------------------------------------------------------------

def test_accepted_goes_only_to_the_owner_and_says_what_it_means(hub, smtp, tmp_path):
    server = smtp()
    src = tmp_path / "q3.html"
    src.write_text("<h1>Q3</h1>")
    art = arts.publish(hub, hub="sales", owner=OWNER, owner_account="id-priya", source=src,
                       title="Q3 report")
    result = _send(hub, artifact_ids=[art["id"]], idem_key="run-1:4")
    assert result["status"] == "accepted" and result["sent"] is True
    assert "not confirmed" in result["message"]
    assert server.envelopes[0][1] == [OWNER]
    raw = server.messages[0].decode()
    assert f"To: {OWNER}" in raw and f"/portal/artifacts/{art['id']}" in raw
    assert f"Message-ID: <{result['delivery_id']}@company.com>" in raw
    row = _rows(hub)[0]
    assert (row["status"], row["recipient"], row["attempts"]) == ("accepted", OWNER, 1)


def test_there_is_no_way_to_name_another_recipient():
    import inspect

    from hubzoid.workflows.context import Hub

    params = set(inspect.signature(Hub.send_email).parameters)
    assert not params & {"to", "cc", "bcc", "recipient", "recipients"}
    params = set(inspect.signature(mail.send_to_owner).parameters)
    assert not params & {"to", "cc", "bcc", "recipient", "recipients"}


def test_links_only_to_the_owners_own_reports(hub, smtp, tmp_path):
    smtp()
    src = tmp_path / "x.html"
    src.write_text("x")
    theirs = arts.publish(hub, hub="sales", owner="sam@company.com", owner_account=None,
                          source=src)
    result = _send(hub, artifact_ids=[theirs["id"]])
    assert result["status"] == "refused" and "not a report owned by" in result["message"]


# --- refusals are not success --------------------------------------------------------

def test_missing_configuration_is_refused_with_the_fix(hub):
    result = _send(hub)
    assert result["status"] == "refused" and not result["sent"]
    assert "HUBZOID_SMTP_HOST" in result["message"] and "preview" in result["message"]


@pytest.mark.parametrize("email", ["admin@localhost", "me@box", "me@host.local", "me@x.invalid"])
def test_unusable_recipients_are_refused(hub, smtp, email):
    smtp()
    ident = RunIdentity(email, None, "local", email)
    result = _send(hub, identity=ident)
    assert result["status"] == "refused" and "preview" in result["message"]


def test_preview_writes_the_outbox_and_says_nothing_was_sent(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    ident = RunIdentity("admin@localhost", None, "local", "admin@localhost")
    result = _send(hub, identity=ident, idem_key="run-2:1")
    assert result["status"] == "previewed" and result["sent"] is False
    assert "no email was sent" in result["message"]
    path = result["outbox"]
    assert "/.hubzoid/outbox/" in path and open(path).read().count("admin@localhost")
    again = _send(hub, identity=ident, idem_key="run-2:1")          # replayed step
    assert again["delivery_id"] == result["delivery_id"] and again.get("repeated")



def test_preview_files_are_owner_only_from_creation(hub, monkeypatch):
    """Even with a permissive umask, and an outbox left 0755 by an older release,
    the email file is created 0600 and its folders are 0700."""
    import os
    import stat

    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    legacy = Path(hub) / ".hubzoid" / "outbox"
    legacy.mkdir(parents=True, exist_ok=True)
    os.chmod(legacy, 0o755)
    old = os.umask(0)
    try:
        ident = RunIdentity("admin@localhost", None, "local", "admin@localhost")
        result = _send(hub, identity=ident, idem_key="run-perm:1")
    finally:
        os.umask(old)
    path = Path(result["outbox"])
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
    assert (mode(path), mode(path.parent), mode(legacy)) == (0o600, 0o700, 0o700)

def test_header_injection_and_legacy_identities_are_refused(hub, smtp):
    smtp()
    assert _send(hub, subject="Hi\r\nBcc: eve@evil.example")["status"] == "refused"
    legacy = RunIdentity("workflow:md:sync", None, "legacy-service")
    result = _send(hub, identity=legacy)
    assert result["status"] == "refused" and "person" in result["message"]


def test_credentials_are_never_sent_in_clear(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_SMTP_HOST", "smtp.company.com")
    monkeypatch.setenv("HUBZOID_SMTP_FROM", "hubzoid@company.com")
    monkeypatch.setenv("HUBZOID_SMTP_USERNAME", "mailer")
    monkeypatch.setenv("HUBZOID_SMTP_PASSWORD", "hunter2-secret")
    monkeypatch.setenv("HUBZOID_SMTP_STARTTLS", "false")
    result = _send(hub)
    assert result["status"] == "refused" and "TLS" in result["message"]


def test_blocked_owner_is_refused_before_sending(hub, smtp):
    from hubzoid.workflows.context import email_now

    server = smtp()
    store_for(hub).suspend(OWNER, actor="t")
    result = email_now(str(hub), "sales", ME.to_dict(), "wf", "run", {"subject": "s"})
    assert result["status"] == "refused" and server.connections == 0


# --- retries ----------------------------------------------------------------------------

def test_recipient_rejection_is_final_and_not_retried(hub, smtp):
    server = smtp(rcpt_code=550)
    result = _send(hub)
    assert result["status"] == "failed" and not result["sent"]
    assert server.connections == 1 and server.messages == []


def test_transient_failure_before_data_is_retried(hub, smtp):
    server = smtp(mail_codes=[451])
    result = _send(hub)
    assert result["status"] == "accepted"
    assert server.connections == 2 and len(server.messages) == 1
    assert _rows(hub)[0]["attempts"] == 2


def test_server_rejecting_the_message_is_a_failure(hub, smtp):
    server = smtp(data="reject")
    result = _send(hub)
    assert result["status"] == "failed" and server.messages == []


def test_disconnect_during_data_is_ambiguous_and_never_resent(hub, smtp):
    server = smtp(data="drop")
    result = _send(hub, idem_key="run-3:2")
    assert result["status"] == "ambiguous" and not result["sent"]
    assert "not resent" in result["message"]
    server.data = "ok"
    again = _send(hub, idem_key="run-3:2")                          # recovery re-runs the step
    assert again["status"] == "ambiguous"
    assert server.connections == 1 and server.messages == []


def test_a_crash_mid_send_is_not_repeated(hub, smtp):
    server = smtp()
    first = _send(hub, idem_key="run-4:2")
    # Simulate the process dying after DATA started: the row says "sending".
    with store_for(hub)._engine.begin() as c:
        c.execute(text("UPDATE hz_email_deliveries SET status='sending' WHERE id=:i"),
                  {"i": first["delivery_id"]})
    again = _send(hub, idem_key="run-4:2")
    assert again["status"] == "ambiguous" and server.connections == 1


def test_an_accepted_message_is_not_sent_twice_on_replay(hub, smtp):
    server = smtp()
    first = _send(hub, idem_key="run-5:2")
    second = _send(hub, idem_key="run-5:2")
    assert first["delivery_id"] == second["delivery_id"] and second.get("repeated")
    assert len(server.messages) == 1


def test_a_failure_before_data_can_be_retried_on_replay(hub, smtp):
    server = smtp(rcpt_code=550)
    assert _send(hub, idem_key="run-6:2")["status"] == "failed"
    server.rcpt_code = 250
    assert _send(hub, idem_key="run-6:2")["status"] == "accepted"
    assert len(server.messages) == 1


# --- secrets stay out --------------------------------------------------------------------

class _AuthFails:
    def __init__(self, cfg):
        pass

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        return 220, b"ready"

    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"bad password {password}".encode())

    def quit(self):
        pass


def test_passwords_never_reach_results_records_or_logs(hub, monkeypatch, caplog):
    monkeypatch.setenv("HUBZOID_SMTP_HOST", "smtp.company.com")
    monkeypatch.setenv("HUBZOID_SMTP_FROM", "hubzoid@company.com")
    monkeypatch.setenv("HUBZOID_SMTP_USERNAME", "mailer")
    monkeypatch.setenv("HUBZOID_SMTP_PASSWORD", "hunter2-secret")
    with caplog.at_level(logging.DEBUG):
        result = _send(hub, smtp_factory=_AuthFails)
    assert result["status"] == "failed"
    assert "hunter2-secret" not in str(result) and "hunter2-secret" not in caplog.text
    assert "hunter2-secret" not in str(_rows(hub))


def test_formatted_email_is_safe_and_retains_plain_text_and_report_links():
    from html.parser import HTMLParser

    body = ('# GitZoid\n\n## Updates\n\n- **Fix** the retry loop.\n- Keep a plain fallback.\n\n'
            '<script>alert(1)</script><img src="https://tracker.invalid/pixel">\n\n'
            '[unsafe](javascript:alert) ![pixel](https://tracker.invalid/pixel) '
            '[source](https://example.com/pr/1)')
    msg = mail.build_message(sender=OWNER, recipient=OWNER, subject="Report", body=body,
                             links=[("Full report", "https://reports.company.com/r/1")],
                             message_id="<test@company.com>", body_format="markdown")
    plain, rich = (msg.get_body(preferencelist=(kind,)).get_content() for kind in ("plain", "html"))
    assert "Fix" in plain and "https://reports.company.com/r/1" in plain
    tags = []
    class Inspect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, dict(attrs)))
    Inspect().feed(rich)
    assert any(tag == "h2" for tag, _ in tags) and any(tag == "li" for tag, _ in tags)
    assert not any(tag in {"script", "img", "iframe"} for tag, _ in tags)
    assert all(not attrs.get("href", "").startswith("javascript:") for _, attrs in tags)
    assert 'href="https://example.com/pr/1"' in rich
    assert 'href="https://reports.company.com/r/1"' in rich
    assert "Sign in to open" in rich
    default = mail.build_message(sender=OWNER, recipient=OWNER, subject="Report", body="# Literal",
                                 links=[], message_id="<plain@company.com>")
    assert "<h1" not in default.get_body(preferencelist=("html",)).get_content()


def test_formatted_email_context_preview_replay_and_invalid_format(hub, monkeypatch):
    from email import policy
    from email.parser import BytesParser
    from hubzoid.workflows.context import email_now

    store_for(hub).grant(OWNER, "sales", "use_hub", actor="test")
    monkeypatch.setenv("HUBZOID_EMAIL_DELIVERY", "preview")
    request = {"subject": "Preview", "body": "# GitZoid\n\n## Highlights\n\n- Useful update",
               "body_format": "markdown"}
    result = email_now(str(hub), "sales", ME.to_dict(), "wf", "run", request, "formatted-test")
    assert result["status"] == "previewed"
    msg = BytesParser(policy=policy.default).parsebytes(Path(result["outbox"]).read_bytes())
    assert "<h2" in msg.get_body(preferencelist=("html",)).get_content()
    again = email_now(str(hub), "sales", ME.to_dict(), "wf", "run", request, "formatted-test")
    assert again["delivery_id"] == result["delivery_id"] and again["repeated"]
    refused = email_now(str(hub), "sales", ME.to_dict(), "wf", "run", request | {"body_format": "rtf"})
    assert refused["status"] == "refused"


def test_html_email_keeps_only_email_safe_layout_and_derives_plain_text():
    body = ('<div style="max-width:600px;background:#fff"><h1>GitZoid Digest</h1>'
            '<table role="presentation" cellpadding="0"><tr><td style="color:#12c46a">14</td><td>merged</td></tr></table>'
            '<script>alert(1)</script><style>p{color:red}</style><img src="https://tracker.invalid/p.gif">'
            '<p onclick="steal()" style="background:url(https://tracker.invalid/bg)">Fixed &lt;b&gt; the loop</p>'
            '<a href="javascript:alert(1)">bad</a> <a href="https://example.com/pull/16">#16</a></div>')
    msg = mail.build_message(sender=OWNER, recipient=OWNER, subject="Digest", body=body,
                             links=[("Full digest", "https://reports.company.com/r/1")],
                             message_id="<html@company.com>", body_format="html", footer="You get this because.")
    plain, rich = (msg.get_body(preferencelist=(kind,)).get_content() for kind in ("plain", "html"))
    assert '<td style="color:#12c46a">14</td>' in rich and 'cellpadding="0"' in rich
    for gone in ("<script", "alert(1)", "<style", "color:red", "<img", "onclick", "url(", "javascript:"):
        assert gone not in rich, gone
    assert "Fixed &lt;b&gt; the loop" in rich and 'href="https://example.com/pull/16"' in rich
    assert 'href="https://reports.company.com/r/1"' in rich and "You get this because." in rich
    assert "GitZoid Digest" in plain and "14 merged" in " ".join(plain.split())
    assert "#16 (https://example.com/pull/16)" in plain and "Fixed <b> the loop" in plain
    assert "Full digest: https://reports.company.com/r/1" in plain and plain.rstrip().endswith("You get this because.")
    assert "alert(1)" not in plain and "color:red" not in plain
    once = mail.build_message(sender=OWNER, recipient=OWNER, subject="Digest", links=[("Full digest", "https://r.co/1")],
                              body='<p><a href="https://r.co/1">Open the full digest</a></p>',
                              message_id="<once@company.com>", body_format="html")
    rich, plain = (once.get_body(preferencelist=(kind,)).get_content() for kind in ("html", "plain"))
    assert rich.count("https://r.co/1") == 1 and plain.count("https://r.co/1") == 1  # the layout's own link is enough
