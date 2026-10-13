"""Owner email: a workflow tells the person it runs as that something is ready.

The recipient is always the run's own account email (see `workflows.identity`).
There is no to/cc/bcc parameter anywhere, so neither workflow code nor a model
can address anyone else. Links to published artifacts are authenticated viewer
links (`/portal/artifacts/<id>`), never public-link tokens.

Delivery uses one deployment-wide SMTP sender (`HUBZOID_SMTP_*`; a hub may
override, and Agent X's layered configuration can supply the values from an AWS
secret). `HUBZOID_EMAIL_DELIVERY=preview` writes the message to a private outbox
(`<hub>/.hubzoid/outbox/<person>/`) and says plainly that nothing was sent.
Missing configuration or an unusable recipient (`admin@localhost`, a
single-label domain, a pending or blocked account) is refused, never reported
as success.

Retries. Each send is recorded in `hz_email_deliveries` under a key unique to
its workflow step, before anything is sent:

  * the row moves to `sending` only after the server accepted the recipient,
    just before `DATA`. A failure before that point means nothing could have
    been accepted, so a retry sends again;
  * a crash or disconnect after `DATA` started is **ambiguous**: the message may
    or may not have been accepted. It is never resent automatically;
  * a re-executed step whose message was already accepted returns the recorded
    result instead of sending twice.

`accepted` means the SMTP server took the message for delivery. It does not
mean it reached the inbox; SMTP gives no such confirmation and no exactly-once
guarantee. The Message-ID is derived from the delivery id, so a duplicate is
recognizable. Credentials are sent only over TLS with certificate and hostname
verification, and never appear in results, records or logs.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger("hubzoid.email")

MAX_SUBJECT = 300
MAX_BODY = 1_000_000
MAX_ATTEMPTS = 3          # tries for failures that happen before DATA
_TRUE = ("1", "true", "yes", "on")
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class EmailError(RuntimeError):
    """The email was not accepted for delivery. `result` has the details."""

    def __init__(self, result: dict):
        super().__init__(result["message"])
        self.result = result

    def __reduce__(self):
        # Workflow errors are pickled into the run's history.
        return (self.__class__, (self.result,))


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    starttls: bool
    use_ssl: bool
    timeout: float
    mode: str               # smtp | preview


def config(env=None) -> SmtpConfig:
    env = os.environ if env is None else env
    use_ssl = (env.get("HUBZOID_SMTP_SSL") or "").strip().lower() in _TRUE
    starttls_raw = (env.get("HUBZOID_SMTP_STARTTLS") or "true").strip().lower()
    try:
        port = int(env.get("HUBZOID_SMTP_PORT") or (465 if use_ssl else 587))
    except ValueError:
        port = 465 if use_ssl else 587
    try:
        timeout = float(env.get("HUBZOID_SMTP_TIMEOUT") or 30)
    except ValueError:
        timeout = 30.0
    mode = (env.get("HUBZOID_EMAIL_DELIVERY") or "smtp").strip().lower()
    return SmtpConfig(
        host=(env.get("HUBZOID_SMTP_HOST") or "").strip(), port=port,
        username=(env.get("HUBZOID_SMTP_USERNAME") or "").strip(),
        password=env.get("HUBZOID_SMTP_PASSWORD") or "",
        sender=(env.get("HUBZOID_SMTP_FROM") or "").strip(),
        starttls=(not use_ssl) and starttls_raw in _TRUE, use_ssl=use_ssl,
        timeout=timeout, mode=mode if mode in ("smtp", "preview") else "invalid")


def config_problem(cfg: SmtpConfig) -> str | None:
    """Why this configuration cannot send, or None."""
    if cfg.mode == "invalid":
        return "HUBZOID_EMAIL_DELIVERY must be 'smtp' or 'preview'."
    if cfg.mode == "preview":
        return None
    if not cfg.host or not cfg.sender:
        return ("Email is not configured: set HUBZOID_SMTP_HOST and HUBZOID_SMTP_FROM (and "
                "HUBZOID_SMTP_USERNAME / HUBZOID_SMTP_PASSWORD if the server needs them), or "
                "set HUBZOID_EMAIL_DELIVERY=preview to write emails to a local outbox instead.")
    if "@" not in cfg.sender or "\n" in cfg.sender or "\r" in cfg.sender:
        return "HUBZOID_SMTP_FROM must be a single email address."
    if cfg.username and not (cfg.starttls or cfg.use_ssl):
        return ("HUBZOID_SMTP_USERNAME is set but TLS is off. Hubzoid does not send SMTP "
                "credentials over an unencrypted connection: enable HUBZOID_SMTP_STARTTLS or "
                "HUBZOID_SMTP_SSL.")
    return None


def recipient_problem(email: str | None) -> str | None:
    """Why `email` cannot receive mail, or None."""
    if not email or "@" not in email:
        return "The account this workflow runs as has no email address."
    domain = email.rsplit("@", 1)[1].lower()
    if (domain in ("localhost", "localdomain") or domain.endswith((".local", ".localhost",
                                                                    ".invalid", ".test"))
            or not _DOMAIN.match(domain)):
        return (f"{email} cannot receive email (it is a local or example account). Run the "
                "workflow as a real account (run_as or HUBZOID_WORKFLOW_USER), or set "
                "HUBZOID_EMAIL_DELIVERY=preview to write the email to a local outbox.")
    return None


def _clean(detail: str, cfg: SmtpConfig | None = None) -> str:
    detail = (detail or "").replace("\r", " ").replace("\n", " ")[:300]
    if cfg and cfg.password:
        detail = detail.replace(cfg.password, "[redacted]")
    return detail


def _engine(hub_dir):
    from . import db
    from .migrations import upgrade

    eng = db.operational_engine(Path(hub_dir))
    upgrade(eng, "operational")
    return eng


def _write_private(outbox: Path, folder: Path, path: Path, data: bytes) -> None:
    """Write a previewed email readable only by the account running Hubzoid:
    the folders are 0700 and the file is created 0600, never readable by
    others even for a moment. An existing file is never overwritten."""
    for d in (outbox, folder):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)  # an outbox made by an older release was 0755
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _record(hub_dir, delivery_id: str) -> dict | None:
    with _engine(hub_dir).connect() as c:
        r = c.execute(text("SELECT * FROM hz_email_deliveries WHERE id=:i"),
                      {"i": delivery_id}).fetchone()
    return dict(r._mapping) if r else None


def _by_key(hub_dir, idem_key: str) -> dict | None:
    with _engine(hub_dir).connect() as c:
        r = c.execute(text("SELECT * FROM hz_email_deliveries WHERE idem_key=:k"),
                      {"k": idem_key}).fetchone()
    return dict(r._mapping) if r else None


def _update(hub_dir, delivery_id: str, **values) -> None:
    values["updated"] = time.time()
    sets = ", ".join(f"{k}=:{k}" for k in values)
    with _engine(hub_dir).begin() as c:
        c.execute(text(f"UPDATE hz_email_deliveries SET {sets} WHERE id=:id"),
                  {**values, "id": delivery_id})


def _result(row: dict, message: str, **extra) -> dict:
    return dict(status=row["status"], sent=row["status"] == "accepted",
                delivery_id=row["id"], recipient=row["recipient"], message=message, **extra)


_MESSAGES = {
    "accepted": ("Accepted by the SMTP server for delivery to {to}. Delivery to the inbox "
                 "is not confirmed."),
    "previewed": "Preview only: no email was sent. Saved to {outbox}.",
    "ambiguous": ("Unknown whether the email to {to} was sent: the connection ended while the "
                  "server was receiving it. It is not resent automatically, to avoid a "
                  "duplicate. Check the inbox, and send again only if it did not arrive."),
}


def _artifact_links(hub_dir, owner: str, artifact_ids,
                    account: str | None = None) -> list[tuple[str, str]]:
    from . import artifacts

    out = []
    for aid in artifact_ids or ():
        art = artifacts.get(hub_dir, aid)
        # Owned by this run's account: the same email and, when both are known,
        # the same chat-app account (a replacement account links nothing).
        if (art is None or art.owner != owner
                or (art.owner_account and account and art.owner_account != account)):
            raise ValueError(f"Artifact {aid!r} is not a report owned by {owner}, so it cannot "
                             "be linked in their email.")
        out.append((art.title, artifacts.viewer_url(art.id, hub_dir)))
    return out


def _formatted_body(body: str) -> str:
    """Render opt-in Markdown with fixed email styles and a structural allowlist.

    Escape raw HTML before Markdown conversion, then remove images, unsafe link
    schemes and arbitrary attributes. Only artifact links are appended by the
    delivery layer; embedded Markdown links must use HTTP(S).
    """
    from html.parser import HTMLParser
    from urllib.parse import urlsplit

    import markdown

    styles = {
        "h1": "font-size:24px;line-height:1.2;color:#087f46;margin:0 0 20px",
        "h2": "font:700 12px/1.5 monospace;text-transform:uppercase;letter-spacing:1px;"
              "border-bottom:1px solid #e7e6e2;padding-bottom:10px;margin-top:26px",
        "h3": "font-size:15px;margin-top:22px",
        "p": "margin:12px 0;line-height:1.6",
        "li": "margin:7px 0;line-height:1.6",
        "blockquote": "margin:16px 0;padding:10px 16px;border-left:3px solid #12c46a;background:#f7f8f6",
        "pre": "white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f8f6;padding:12px",
        "a": "color:#087f46",
    }
    allowed = set(styles) | {"ul", "ol", "strong", "em", "code", "br", "hr"}

    class SafeEmail(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag not in allowed:
                return
            attributes = f' style="{styles[tag]}"' if tag in styles else ""
            if tag == "a":
                href = dict(attrs).get("href") or ""
                # Reject control characters and malformed URLs, even if browsers normalize them.
                try:
                    valid = not any(ord(c) < 33 for c in href) and urlsplit(href).scheme.lower() in {"https", "http"}
                except ValueError:
                    valid = False
                if valid:
                    attributes += f' href="{html.escape(href, quote=True)}"'
            self.parts.append(f"<{tag}{attributes}>")

        def handle_endtag(self, tag):
            if tag in allowed and tag not in {"br", "hr"}:
                self.parts.append(f"</{tag}>")

        def handle_data(self, data):
            self.parts.append(html.escape(data))

    parser = SafeEmail()
    parser.feed(markdown.markdown(html.escape(body)))
    parser.close()
    return "".join(parser.parts)


def build_message(*, sender: str, recipient: str, subject: str, body: str,
                  links: list[tuple[str, str]], message_id: str, body_format: str = "text") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = message_id
    text_body = body.rstrip()
    if body_format == "markdown":
        import re
        text_body = re.sub(r"\\([\\`*_{}\[\]()#+.!<>|~-])", r"\1", text_body)
    if links:
        text_body += "\n\n" + "\n".join(f"{title}: {url}" for title, url in links)
        text_body += "\n\n(Sign in to open these reports.)"
    msg.set_content(text_body + "\n")
    items = "".join(f'<li><a href="{html.escape(url)}">{html.escape(title)}</a></li>'
                    for title, url in links)
    if body_format not in {"text", "markdown"}:
        raise ValueError("Email body_format must be text or markdown.")
    content = (_formatted_body(body) if body_format == "markdown" else
               "".join(f"<p>{html.escape(p)}</p>" for p in body.strip().split("\n\n") if p.strip()))
    html_body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                 "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"></head>"
                 "<body style=\"margin:0;background:#f2f3f1;color:#0b0b0c\">"
                 "<div style=\"font-family:system-ui,Arial,sans-serif;font-size:14px;line-height:1.5;"
                 "max-width:640px;margin:24px auto;padding:24px;background:#fff;"
                 "border:1px solid #e7e6e2;border-radius:14px;overflow-wrap:anywhere\">" + content
                 + (f"<hr style=\"border:0;border-top:1px solid #e7e6e2;margin-top:24px\">"
                    f"<p><strong>Open the full report</strong></p><ul>{items}</ul>"
                    "<p style=\"color:#5f6773;font-size:12px\">Sign in to open these reports.</p>"
                    if links else "") + "</div></body></html>")
    if body_format == "text":
        # Preserve the existing presentation for workflows that have not opted in.
        html_body = ("<div style=\"font-family:system-ui,Arial,sans-serif;font-size:14px;"
                     "line-height:1.5\">" + content
                     + (f"<ul>{items}</ul><p style=\"color:#666\">Sign in to open these reports.</p>"
                        if links else "") + "</div>")
    msg.add_alternative(html_body, subtype="html")
    return msg


class _NotSent(Exception):
    """Failed before DATA: nothing can have been accepted."""

    def __init__(self, detail: str, code: int | None = None, permanent: bool = False):
        super().__init__(detail)
        self.detail, self.code, self.permanent = detail, code, permanent


class _Ambiguous(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class _Rejected(Exception):
    """The server answered DATA with an error: it did not accept the message."""

    def __init__(self, detail: str, code: int):
        super().__init__(detail)
        self.detail, self.code = detail, code


def _smtp_send(cfg: SmtpConfig, msg: EmailMessage, recipient: str, on_data, smtp_factory):
    """One SMTP transaction in explicit stages, so a failure is classified by
    where it happened. Returns the DATA reply code."""
    ctx = ssl.create_default_context()
    try:
        if smtp_factory is not None:
            smtp = smtp_factory(cfg)
        elif cfg.use_ssl:
            smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=ctx)
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
    except (OSError, smtplib.SMTPException) as exc:
        raise _NotSent(f"could not connect to {cfg.host}:{cfg.port}: {type(exc).__name__}")
    try:
        try:
            smtp.ehlo()
            if cfg.starttls:
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if cfg.username:
                smtp.login(cfg.username, cfg.password)
            code, resp = smtp.mail(cfg.sender)
            if code != 250:
                raise _NotSent(f"sender refused: {code} {resp!r}", code, permanent=code >= 500)
            code, resp = smtp.rcpt(recipient)
            if code not in (250, 251):
                raise _NotSent(f"recipient refused: {code} {resp!r}", code, permanent=code >= 500)
        except smtplib.SMTPAuthenticationError as exc:
            raise _NotSent(f"authentication failed ({exc.smtp_code})", exc.smtp_code, permanent=True)
        except smtplib.SMTPResponseException as exc:
            raise _NotSent(f"{type(exc).__name__} {exc.smtp_code}", exc.smtp_code,
                           permanent=exc.smtp_code >= 500)
        except (OSError, smtplib.SMTPException) as exc:
            raise _NotSent(f"{type(exc).__name__} before sending")
        on_data()
        try:
            code, resp = smtp.data(msg.as_bytes())
        except smtplib.SMTPResponseException as exc:
            raise _Rejected(f"message refused: {exc.smtp_code}", exc.smtp_code)
        except (OSError, smtplib.SMTPException) as exc:
            raise _Ambiguous(f"{type(exc).__name__} while the server was receiving the message")
        if code != 250:
            raise _Rejected(f"message refused: {code} {resp!r}", code)
        return code
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 — after acceptance a failed QUIT changes nothing
            pass


def send_to_owner(hub_dir, *, hub: str, identity, subject: str, body: str,
                  artifact_ids=(), workflow: str | None = None, run_id: str | None = None,
                  idem_key: str | None = None, smtp_factory=None, sleep=time.sleep,
                  body_format: str = "text") -> dict:
    """Send (or preview) one email to the run's own account. Returns a result
    dict: status accepted | previewed | failed | ambiguous | refused, `sent`
    (True only for accepted), `delivery_id`, `recipient` and a `message`
    that says exactly what happened. Never raises for a delivery outcome."""
    from .workflows.identity import IdentityError, require_person

    try:
        require_person(identity, "Sending email")
    except IdentityError as exc:
        return dict(status="refused", sent=False, delivery_id=None, recipient=None,
                    message=str(exc))
    owner = identity.subject
    recipient = (identity.email or owner or "").strip().lower()
    subject = (subject or "").strip()
    if not subject or len(subject) > MAX_SUBJECT or "\n" in subject or "\r" in subject:
        return dict(status="refused", sent=False, delivery_id=None, recipient=recipient,
                    message=f"The subject must be one line of at most {MAX_SUBJECT} characters.")
    if body_format not in {"text", "markdown"}:
        return dict(status="refused", sent=False, delivery_id=None, recipient=None,
                    message="Email body_format must be text or markdown.")
    body = body or ""
    if len(body) > MAX_BODY:
        return dict(status="refused", sent=False, delivery_id=None, recipient=recipient,
                    message="The email body is too long; publish the content as a report and link it.")
    try:
        links = _artifact_links(hub_dir, owner, artifact_ids, identity.account_id)
    except ValueError as exc:
        return dict(status="refused", sent=False, delivery_id=None, recipient=recipient,
                    message=str(exc))
    return _deliver(hub_dir, hub=hub, owner=owner, recipient=recipient, subject=subject, body=body,
                    links=links, artifact_ids=artifact_ids, workflow=workflow, run_id=run_id,
                    idem_key=idem_key, smtp_factory=smtp_factory, sleep=sleep, body_format=body_format)


def _deliver(hub_dir, *, hub: str, owner: str, recipient: str, subject: str, body: str,
             links, artifact_ids, workflow, run_id, idem_key, smtp_factory, sleep, body_format="text") -> dict:
    """Send (or preview) one checked message to one recipient, recorded under
    `idem_key` so a retry never sends an accepted message twice."""
    cfg = config()
    problem = config_problem(cfg)
    if problem is None and cfg.mode == "smtp":
        problem = recipient_problem(recipient)
    if problem:
        log.warning("email: not sent for %s: %s", owner, problem)
        return dict(status="refused", sent=False, delivery_id=None, recipient=recipient,
                    message=problem)

    row = _by_key(hub_dir, idem_key) if idem_key else None
    if row is not None:
        if row["status"] in ("accepted", "previewed", "ambiguous"):
            return _result(row, _MESSAGES[row["status"]].format(
                to=row["recipient"], outbox=row.get("detail") or "the outbox"),
                           repeated=True)
        if row["status"] == "sending":
            _update(hub_dir, row["id"], status="ambiguous",
                    detail="interrupted while the server was receiving the message")
            row = _record(hub_dir, row["id"])
            return _result(row, _MESSAGES["ambiguous"].format(to=row["recipient"]))
        # pending / connecting / failed: nothing was accepted, so try again.
        delivery_id = row["id"]
    else:
        delivery_id = "e" + secrets.token_urlsafe(12)
        now = time.time()
        with _engine(hub_dir).begin() as c:
            c.execute(text(
                "INSERT INTO hz_email_deliveries (id, idem_key, hub, workflow, run_id, owner, "
                "recipient, subject, artifacts, mode, status, attempts, created, updated) VALUES "
                "(:id, :k, :hub, :wf, :run, :owner, :to, :subj, :arts, :mode, 'pending', 0, :t, :t)"),
                dict(id=delivery_id, k=idem_key, hub=hub, wf=workflow, run=run_id, owner=owner,
                     to=recipient, subj=subject, arts=json.dumps(list(artifact_ids or [])),
                     mode=cfg.mode, t=now))
    domain = (cfg.sender.rsplit("@", 1)[-1] if "@" in cfg.sender else "hubzoid.local")
    message_id = f"<{delivery_id}@{domain}>"
    msg = build_message(sender=cfg.sender or "hubzoid-preview@localhost", recipient=recipient,
                        subject=subject, body=body, links=links, message_id=message_id, body_format=body_format)

    if cfg.mode == "preview":
        from .workflows.identity import person_slug

        outbox = Path(hub_dir) / ".hubzoid" / "outbox"
        folder = outbox / person_slug(owner)
        path = folder / f"{time.strftime('%Y%m%dT%H%M%S')}-{delivery_id}.eml"
        _write_private(outbox, folder, path, msg.as_bytes())
        _update(hub_dir, delivery_id, status="previewed", detail=str(path), message_id=message_id)
        log.info("email: preview for %s written to %s (not sent)", owner, path)
        return _result(_record(hub_dir, delivery_id), _MESSAGES["previewed"].format(outbox=path),
                       outbox=str(path))

    attempts = (row or {}).get("attempts") or 0
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts += 1
        _update(hub_dir, delivery_id, status="connecting", attempts=attempts,
                message_id=message_id)
        try:
            code = _smtp_send(cfg, msg, recipient,
                              lambda: _update(hub_dir, delivery_id, status="sending"),
                              smtp_factory)
        except _NotSent as exc:
            last = exc
            _update(hub_dir, delivery_id, status="failed", detail=_clean(exc.detail, cfg),
                    smtp_code=exc.code)
            log.warning("email: attempt %d to %s failed before sending: %s", attempt,
                        recipient, _clean(exc.detail, cfg))
            if exc.permanent or attempt == MAX_ATTEMPTS:
                break
            sleep(min(2 ** attempt, 10))
            continue
        except _Rejected as exc:
            _update(hub_dir, delivery_id, status="failed", detail=_clean(exc.detail, cfg),
                    smtp_code=exc.code)
            row = _record(hub_dir, delivery_id)
            return _result(row, f"The SMTP server refused the email to {recipient} "
                                f"({exc.code}). It was not sent.")
        except _Ambiguous as exc:
            _update(hub_dir, delivery_id, status="ambiguous", detail=_clean(exc.detail, cfg))
            log.error("email: outcome unknown for %s: %s", recipient, _clean(exc.detail, cfg))
            return _result(_record(hub_dir, delivery_id),
                           _MESSAGES["ambiguous"].format(to=recipient))
        _update(hub_dir, delivery_id, status="accepted", smtp_code=code, detail=None)
        log.info("email: accepted by the SMTP server for %s (%s)", recipient, delivery_id)
        return _result(_record(hub_dir, delivery_id),
                       _MESSAGES["accepted"].format(to=recipient))
    row = _record(hub_dir, delivery_id)
    detail = _clean(last.detail, cfg) if last else "unknown error"
    return _result(row, f"The email to {recipient} was not sent: {detail}.")


MAX_LIST = 50
MAX_LIST_BODY = 2000


def list_members(hub_dir, name: str) -> list[str]:
    """The addresses of `email_lists.<name>` in workflows/settings.yaml."""
    from .workflows.events import settings

    lists = (settings(Path(hub_dir)).get("email_lists") or {})
    if not isinstance(lists, dict) or name not in lists:
        raise ValueError(f"There is no email list named {name!r} in workflows/settings.yaml.")
    members = lists[name]
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
        raise ValueError(f"Email list {name!r} must be a list of addresses.")
    out = sorted({m.strip().lower() for m in members if m.strip()})
    if len(out) > MAX_LIST:
        raise ValueError(f"Email list {name!r} has more than {MAX_LIST} addresses.")
    return out


def _list_link(hub_dir, art) -> str:
    from . import artifacts
    from .artifacts import series as series_lib

    if art.series_id:
        found = series_lib.get(hub_dir, art.series_id)
        if found is not None:
            token = series_lib.domain_token(hub_dir, found)
            return series_lib.share_url(hub_dir, token) if token else series_lib.latest_url(hub_dir, found)
    return artifacts.viewer_url(art.id, hub_dir)


def _list_footer(list_name: str, hub: str, body_format: str) -> str:
    footer = f"You get this because you're on the {list_name} list for {hub}. To stop, ask the agent's admin."
    if body_format == "markdown":
        import re
        footer = re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", " ".join(footer.split()))
    return footer


def send_to_list(hub_dir, *, hub: str, identity, list_name: str, subject: str, body: str,
                 artifact_ids=(), workflow: str | None = None, run_id: str | None = None,
                 idem_key: str | None = None, smtp_factory=None, sleep=time.sleep,
                  body_format: str = "text") -> dict:
    """Email a builder-declared list, one message per address, so nobody sees
    who else is on it. Every address must belong to an account here or to the
    company's share domains. The bounded body is text or opt-in Markdown; full
    report content stays behind sign-in, at its link. Returns {status, sent, results, message}."""
    from . import artifacts
    from .access import store_for
    from .artifacts import domains
    from .workflows.identity import IdentityError, require_person

    try:
        require_person(identity, "Sending email")
        members = list_members(hub_dir, list_name)
    except (IdentityError, ValueError) as exc:
        return dict(status="refused", sent=False, results=[], message=str(exc))
    subject = (subject or "").strip()
    if not subject or len(subject) > MAX_SUBJECT or "\n" in subject or "\r" in subject:
        return dict(status="refused", sent=False, results=[],
                    message=f"The subject must be one line of at most {MAX_SUBJECT} characters.")
    if body_format not in {"text", "markdown"}:
        return dict(status="refused", sent=False, results=[],
                    message="Email body_format must be text or markdown.")
    body = body or ""
    if len(body) > MAX_LIST_BODY:
        return dict(status="refused", sent=False, results=[],
                    message=f"A list email body is at most {MAX_LIST_BODY} characters. Link the report instead.")
    links = []
    for aid in artifact_ids or ():
        art = artifacts.get(hub_dir, aid)
        if art is None or art.owner != identity.subject:
            return dict(status="refused", sent=False, results=[],
                        message=f"Artifact {aid!r} is not a report this run published.")
        links.append((art.title, _list_link(hub_dir, art)))
    gs = store_for(Path(hub_dir))
    share = set(domains.share_domains())
    results = []
    for member in members:
        known = bool((gs.identity(member) or {}).get("owui_id"))
        if gs.is_suspended(member):
            results.append(dict(status="skipped", recipient=member, message="Blocked account."))
            continue
        if not known and member.rsplit("@", 1)[-1] not in share:
            results.append(dict(status="refused", recipient=member,
                                message="Not an account here and not at a company share domain."))
            continue
        results.append(_deliver(
            hub_dir, hub=hub, owner=identity.subject, recipient=member, subject=subject,
            body=body + "\n\n" + _list_footer(list_name, hub, body_format),
            links=links, artifact_ids=artifact_ids, workflow=workflow, run_id=run_id,
            idem_key=f"{idem_key}:{member}" if idem_key else None, smtp_factory=smtp_factory,
            sleep=sleep, body_format=body_format))
    ok = [r for r in results if r.get("status") in ("accepted", "previewed")]
    status = ("refused" if not ok else "previewed" if all(r["status"] == "previewed" for r in ok)
              else "accepted")
    if ok and len(ok) < len(results):
        status = "partial"
    return dict(status=status, sent=any(r.get("status") == "accepted" for r in results),
                results=results, message=f"{len(ok)} of {len(results)} sent to the {list_name} list.")
