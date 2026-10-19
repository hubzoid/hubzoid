"""Reading a raw email safely, and deciding whether its sender is who it says.

Parsing uses the standard library (``email.parser.BytesParser`` with the
default policy) under hard limits: 30 MiB raw, 256 MIME parts, nesting depth
10 and 256 KiB of decoded text. Anything structurally doubtful (two From
addresses, an encoded-word trick in a header, a part past the limits) is
quarantined with the reason. HTML-only mail becomes inert text: scripts,
styles and images are dropped and nothing remote is ever loaded.

The sender gate (``aligned_dkim``) admits only a single From address on the
exact allowlist whose message carries a valid DKIM signature from the same
domain, covering From and Subject, without a body-length limit and not
rsa-sha1 (dkimpy does the verification). ``Authentication-Results`` headers
in the message are never trusted, as OpenClaw's sender gate also avoids
(``extensions/imap/src/sender-gate.ts:161-216``). DKIM proves the domain
sent it, not who the person is: the work runs as the workflow, never as the
sender.
"""
from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from email import policy
from email.utils import getaddresses
from html.parser import HTMLParser

MAX_RAW = 30 * 1024 * 1024
MAX_PARTS = 256
MAX_DEPTH = 10
MAX_TEXT = 256 * 1024
MSGID_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")


@dataclass
class Parsed:
    message_id: str | None
    from_addr: str | None
    from_name: str
    subject: str
    text: str
    in_reply_to: str | None
    references: list[str]
    date: str
    attachments: list[dict] = field(default_factory=list)   # {part, name, mimetype, size}
    problems: list[str] = field(default_factory=list)
    auto_submitted: bool = False
    raw_sha256: str = ""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - broken HTML still gives what it parsed
        pass
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def normalize_address(addr: str) -> str:
    """Lowercase and IDNA-normalize the domain; keep the local part as sent."""
    local, _, domain = (addr or "").strip().rpartition("@")
    if not local or not domain:
        return (addr or "").strip().lower()
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        domain = domain.lower()
    return f"{local}@{domain}"


def parse(raw: bytes) -> Parsed:
    problems: list[str] = []
    if len(raw) > MAX_RAW:
        return Parsed(None, None, "", "", "", None, [], "", problems=["message_too_large"],
                      raw_sha256="sha256:" + hashlib.sha256(raw[:1024]).hexdigest())
    msg = email.message_from_bytes(raw, policy=policy.default)
    froms = getaddresses(msg.get_all("From", []))
    if len(froms) != 1 or not froms[0][1]:
        problems.append("from_not_single")
    # A second copy of a signed header lets the unsigned one be read instead.
    if any(len(msg.get_all(h, [])) > 1 for h in ("From", "Subject", "Date", "Message-ID", "To", "Reply-To")):
        problems.append("header_duplicated")
    name, addr = froms[0] if froms else ("", "")
    msgid_raw = (msg.get("Message-ID") or "").strip()
    m = MSGID_RE.search(msgid_raw)
    message_id = m.group(0) if m else None
    if not message_id:
        problems.append("message_id_missing")
    refs = MSGID_RE.findall(msg.get("References") or "")[:100]
    irt = MSGID_RE.search(msg.get("In-Reply-To") or "")
    auto = (msg.get("Auto-Submitted") or "no").strip().lower() not in ("", "no") or \
        (msg.get("Precedence") or "").lower() in ("bulk", "list", "junk") or bool(msg.get("List-Id"))
    text_parts, html_parts, attachments = [], [], []
    count = 0

    def walk(part, depth):
        nonlocal count
        count += 1
        if count > MAX_PARTS or depth > MAX_DEPTH:
            problems.append("mime_too_complex")
            return
        disp = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        filename = part.get_filename()
        # Classify before recursing: an attached email is multipart in Python,
        # and its body must not become this message's text.
        if ctype == "message/rfc822" or (part.is_multipart() and (disp == "attachment" or filename)):
            payload = part.as_bytes() if ctype != "message/rfc822" else _rfc822_bytes(part)
            safe = re.sub(r"[^A-Za-z0-9._ -]", "_", (filename or f"message-{count}.eml").rsplit("/", 1)[-1])[:200]
            attachments.append({"part": str(count), "name": safe, "mimetype": "message/rfc822", "size": len(payload)})
            return
        if part.is_multipart():
            for sub in part.iter_parts():
                walk(sub, depth + 1)
            return
        if disp == "attachment" or filename or not ctype.startswith("text/"):
            payload = part.get_payload(decode=True) or b""
            safe = re.sub(r"[^A-Za-z0-9._ -]", "_", (filename or f"part-{count}").rsplit("/", 1)[-1])[:200]
            attachments.append({"part": str(count), "name": safe or f"part-{count}", "mimetype": ctype,
                                "size": len(payload)})
            return
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            problems.append("unsupported_encoding")
            return
        (html_parts if ctype == "text/html" else text_parts).append(content if isinstance(content, str) else "")

    walk(msg, 0)
    text = "\n\n".join(t for t in text_parts if t.strip()) or html_to_text("\n".join(html_parts))
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
        problems.append("text_truncated")
    return Parsed(message_id=message_id, from_addr=normalize_address(addr) if addr else None, from_name=name or "",
                  subject=str(msg.get("Subject") or "")[:500], text=text.strip(), in_reply_to=irt.group(0) if irt else None,
                  references=refs, date=str(msg.get("Date") or ""), attachments=attachments, problems=problems,
                  auto_submitted=auto, raw_sha256="sha256:" + hashlib.sha256(raw).hexdigest())


def attachment_bytes(raw: bytes, part: str) -> tuple[str, str, bytes]:
    """The bytes of one attachment, by the part number `parse` gave it."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    count = 0
    found = None

    def walk(p):
        nonlocal count, found
        count += 1
        if found is not None:
            return
        disp = (p.get_content_disposition() or "").lower()
        whole = p.get_content_type() == "message/rfc822" or (p.is_multipart() and (disp == "attachment"
                                                                                     or p.get_filename()))
        if whole or not p.is_multipart():
            if str(count) == str(part):
                found = p
            return
        for sub in p.iter_parts():
            walk(sub)

    walk(msg)
    if found is None:
        raise KeyError(part)
    if found.get_content_type() == "message/rfc822":
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", (found.get_filename() or f"message-{part}.eml"))[:200]
        return name, "message/rfc822", _rfc822_bytes(found)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (found.get_filename() or f"part-{part}").rsplit("/", 1)[-1])[:200]
    return name, found.get_content_type(), found.get_payload(decode=True) or b""


def _rfc822_bytes(part) -> bytes:
    inner = part.get_payload()
    if isinstance(inner, list) and inner:
        inner = inner[0]
    try:
        return inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(str(inner), "utf-8")
    except Exception:  # noqa: BLE001 - an unreadable attached email is still kept as text
        return str(inner).encode("utf-8", "replace")


def sender_trust(raw: bytes, parsed: Parsed, *, mailbox: str, allowed: list[str], authentication: str,
                 dnsfunc=None) -> dict:
    """{"decision": accepted | quarantined | rejected, "reason", "evidence"}."""
    if parsed.problems and any(p in parsed.problems for p in ("from_not_single", "header_duplicated",
                                                              "mime_too_complex", "message_too_large",
                                                              "unsupported_encoding")):
        return {"decision": "quarantined", "reason": parsed.problems[0], "evidence": {}}
    sender = parsed.from_addr or ""
    if sender == normalize_address(mailbox):
        return {"decision": "rejected", "reason": "own_mailbox", "evidence": {}}
    if parsed.auto_submitted:
        return {"decision": "rejected", "reason": "automatic_message", "evidence": {}}
    if sender not in {normalize_address(a) for a in allowed}:
        return {"decision": "quarantined", "reason": "unknown_sender", "evidence": {"from": sender}}
    if authentication != "aligned_dkim":
        return {"decision": "quarantined", "reason": "manual_review", "evidence": {"from": sender}}
    ok, evidence = verify_dkim(raw, sender, dnsfunc=dnsfunc)
    if not ok:
        return {"decision": "quarantined", "reason": evidence.get("reason", "sender_unverified"), "evidence": evidence}
    return {"decision": "accepted", "reason": "aligned_dkim", "evidence": evidence}


def verify_dkim(raw: bytes, sender: str, *, dnsfunc=None) -> tuple[bool, dict]:
    import dkim

    domain = sender.rpartition("@")[2]
    try:
        signatures = [s for s in email.message_from_bytes(raw).get_all("DKIM-Signature") or []]
    except Exception:  # noqa: BLE001
        signatures = []
    if not signatures:
        return False, {"reason": "sender_unverified", "detail": "no DKIM signature"}
    for sig in signatures:
        tags = dict(t.strip().split("=", 1) for t in re.sub(r"\s+", "", sig).split(";") if "=" in t)
        if tags.get("d", "").lower() != domain:
            continue
        if "l" in tags:
            return False, {"reason": "sender_unverified", "detail": "body length limit"}
        if tags.get("a", "").lower() == "rsa-sha1":
            return False, {"reason": "sender_unverified", "detail": "rsa-sha1"}
        signed = {h.strip().lower() for h in tags.get("h", "").split(":")}
        if not {"from", "subject"} <= signed:
            return False, {"reason": "sender_unverified", "detail": "From or Subject not signed"}
        try:
            d = dkim.DKIM(raw, minkey=1024, timeout=5)
            idx = signatures.index(sig)
            ok = d.verify(idx=idx, dnsfunc=dnsfunc) if dnsfunc else d.verify(idx=idx)
        except dkim.DKIMException as exc:      # the signature or key is wrong: it does not verify
            return False, {"reason": "sender_unverified", "detail": str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001 - a DNS failure is retried, never admitted
            return False, {"reason": "dkim_unavailable", "detail": type(exc).__name__}
        if ok:
            return True, {"d": domain, "s": tags.get("s"), "a": tags.get("a")}
        return False, {"reason": "sender_unverified", "detail": "signature does not verify"}
    return False, {"reason": "sender_unverified", "detail": "no signature from the sender's domain"}


def thread_root(parsed: Parsed) -> str | None:
    """The first message of the conversation this one belongs to."""
    if parsed.references:
        return parsed.references[0]
    return parsed.in_reply_to or parsed.message_id


def build_reply(*, mailbox: str, to: str, subject: str, body: str, in_reply_to: str | None, references: list[str],
                message_id: str) -> bytes:
    """An RFC 5322 reply: From the mailbox, To the admitted sender only, in the
    same conversation. No arbitrary recipients, cc or bcc."""
    from email.message import EmailMessage
    from email.utils import formatdate

    msg = EmailMessage()
    msg["From"] = mailbox
    msg["To"] = to
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        chain = [r for r in references if r != in_reply_to][-20:] + [in_reply_to]
        msg["References"] = " ".join(chain)
    msg.set_content(body)
    return bytes(msg)

