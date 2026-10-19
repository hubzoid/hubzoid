"""The three ways a mailbox is read and written: IMAP with SMTP, the Gmail
API and Microsoft Graph.

Each transport has the same five calls:

* ``fetch_new(conn, cursor)`` returns ``(messages, cursor)`` where each message
  is ``{"id", "raw", "thread_id"}``. A ``None`` cursor starts from now: the
  existing mail is never read (``start: new_only``).
* ``send(conn, raw, to, thread_id)`` sends one approved message.
* ``save_draft(conn, raw, message_id, thread_id)`` saves a real draft and reads
  it back.
* ``find_sent(conn, message_id)`` looks for a sent message by its Message-ID:
  the provider's id, ``False`` when it is certainly not there, ``None`` when it
  cannot look.
* ``doctor(conn)`` reads only.

Sends map provider answers to the work-item outcomes: ``NotSent`` when the
message provably never left (no connection, refused login, rate limit),
``Rejected`` when the provider refused it, and any other failure stays
uncertain, so it is never sent again without a person.

The ``HUBZOID_GMAIL_API_URL``, ``HUBZOID_GRAPH_API_URL`` and
``HUBZOID_EMAIL_CA_FILE`` overrides exist for a local provider sandbox. They
are operator settings, and doctor names them when set.
"""
from __future__ import annotations

import base64
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone

from ..errors import ChannelError
from ..protocol import Check

log = logging.getLogger("hubzoid.channels.email")

MAX_RAW = 30 * 1024 * 1024
BATCH = 20
OVERRIDES = ("HUBZOID_GMAIL_API_URL", "HUBZOID_GRAPH_API_URL", "HUBZOID_EMAIL_CA_FILE", "HUBZOID_GOOGLE_TOKEN_URL",
             "HUBZOID_MS_LOGIN_URL")

_client = None


def http():
    """The HTTP client for Gmail, Graph and token calls. Tests replace it."""
    global _client
    if _client is None:
        import httpx

        _client = httpx.Client(follow_redirects=False, timeout=30)
    return _client


def for_conn(conn):
    return {"imap": ImapSmtp, "gmail": Gmail, "microsoft365": Graph}[conn.settings.transport]()


def _not_sent(msg: str):
    from ...work_items.errors import NotSent

    return NotSent(msg)


def _rejected(msg: str):
    from ...work_items.errors import Rejected

    return Rejected(msg)


def _override_checks() -> list[Check]:
    set_ = [k for k in OVERRIDES if os.environ.get(k)]
    if not set_:
        return []
    return [Check("email:overrides", "warn", "Provider addresses are overridden: " + ", ".join(set_) + ". Use these "
                  "only with a local sandbox.", "provider_override", "Unset them for a real mailbox.")]


# -- IMAP and SMTP -------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca = os.environ.get("HUBZOID_EMAIL_CA_FILE")
    if ca:
        ctx.load_verify_locations(ca)
    return ctx


def _imap_connect(conn):
    """A logged-in IMAP client. Tests replace this."""
    from imapclient import IMAPClient

    c = conn.settings.imap
    client = IMAPClient(c.host, port=c.port, ssl=c.tls == "implicit", ssl_context=_tls_context(), timeout=30)
    if c.tls == "starttls":
        client.starttls(_tls_context())
    client.login(c.username, _password(conn, "imap"))
    return client


def _smtp_connect(conn):
    """A logged-in SMTP client. Tests replace this."""
    import smtplib

    c = conn.settings.smtp
    if c.tls == "implicit":
        client = smtplib.SMTP_SSL(c.host, c.port, context=_tls_context(), timeout=30)
    else:
        client = smtplib.SMTP(c.host, c.port, timeout=30)
        client.starttls(context=_tls_context())
    client.login(c.username, _password(conn, "smtp"))
    return client


def _password(conn, section: str) -> str:
    from ...secretstore import resolve

    ref = getattr(conn.settings, section).password_ref
    try:
        return resolve(ref, conn.hub_dir)
    except Exception as exc:  # noqa: BLE001
        raise ChannelError("credential_missing", f"Set the secret {ref} first.", status=503) from exc


def _key(d: dict, name: str):
    return d.get(name.encode()) if name.encode() in d else d.get(name)


class ImapSmtp:
    name = "imap"

    def fetch_new(self, conn, cursor: dict | None):
        c = conn.settings.imap
        client = _imap_connect(conn)
        try:
            info = client.select_folder(c.folder, readonly=True)       # never marks anything read
            validity = int(_key(info, "UIDVALIDITY") or 0)
            uidnext = int(_key(info, "UIDNEXT") or 1)
            if cursor is None:
                return [], {"uidvalidity": validity, "last_uid": uidnext - 1, "since": _today()}
            if int(cursor.get("uidvalidity") or 0) != validity:
                # The server renumbered the folder. Read the recent days again;
                # admission recognises every message already seen by its Message-ID.
                log.warning("channel %s: UIDVALIDITY changed; re-reading since %s", conn.id, cursor.get("since"))
                uids = sorted(client.search(["SINCE", _date(cursor.get("since"))]))[-BATCH:]
            else:
                last = int(cursor.get("last_uid") or 0)
                uids = [u for u in client.search(["UID", f"{last + 1}:*"]) if u > last]
                uids = sorted(uids)[:BATCH]
            out = []
            if uids:
                sizes = client.fetch(uids, ["RFC822.SIZE"])
                small = [u for u in uids if int(_key(sizes.get(u, {}), "RFC822.SIZE") or 0) <= MAX_RAW]
                bodies = client.fetch(small, ["BODY.PEEK[]"]) if small else {}
                for u in uids:
                    raw = _key(bodies.get(u, {}), "BODY[]") if u in small else None
                    out.append({"id": f"imap:{validity}:{u}", "raw": raw or b"", "too_large": u not in small,
                                "thread_id": None})
            last_uid = max([int(cursor.get("last_uid") or 0)] + list(uids)) if int(
                cursor.get("uidvalidity") or 0) == validity else max([uidnext - 1] + list(uids))
            return out, {"uidvalidity": validity, "last_uid": last_uid, "since": _today()}
        finally:
            _logout(client)

    def send(self, conn, raw: bytes, *, to: str, thread_id=None) -> str:
        import smtplib

        try:
            client = _smtp_connect(conn)
        except smtplib.SMTPAuthenticationError:
            raise _rejected("blocked_credentials: the SMTP server refused the login.") from None
        except (OSError, smtplib.SMTPException) as exc:
            raise _not_sent(f"Could not reach the SMTP server ({type(exc).__name__}).") from None
        try:
            client.sendmail(conn.settings.mailbox, [to], raw)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise _rejected(f"The SMTP server refused the message ({type(exc).__name__}).") from None
        except smtplib.SMTPDataError as exc:
            if 400 <= exc.smtp_code < 500:
                raise _not_sent(f"The SMTP server deferred the message ({exc.smtp_code}).") from None
            raise _rejected(f"The SMTP server refused the message ({exc.smtp_code}).") from None
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass
        return "smtp"

    def save_draft(self, conn, raw: bytes, *, message_id: str, thread_id=None) -> str:
        c = conn.settings.imap
        folder = c.drafts_folder
        client = _imap_connect(conn)
        try:
            if not folder:
                folder = _special(client, b"\\Drafts")
            if not folder:
                raise _not_sent("draft_unsupported: the mailbox has no Drafts folder. Set imap.drafts_folder.")
            client.append(folder, raw, flags=[b"\\Draft"])
            client.select_folder(folder, readonly=True)
            found = client.search(["HEADER", "Message-ID", message_id])
            if not found:
                raise RuntimeError("the draft was saved but could not be read back")
            return f"imap-draft:{folder}:{found[-1]}"
        finally:
            _logout(client)

    def find_sent(self, conn, message_id: str):
        client = _imap_connect(conn)
        try:
            folder = _special(client, b"\\Sent")
            if not folder:
                return None
            client.select_folder(folder, readonly=True)
            found = client.search(["HEADER", "Message-ID", message_id])
            return f"imap-sent:{found[-1]}" if found else False
        finally:
            _logout(client)

    def whoami(self, token: str):
        return None

    def doctor(self, conn) -> list[Check]:
        import smtplib

        out = _override_checks()
        try:
            client = _imap_connect(conn)
            try:
                client.select_folder(conn.settings.imap.folder, readonly=True)
                out.append(Check("email:imap", "ok", f"Signed in to IMAP and opened {conn.settings.imap.folder} "
                                 "read-only."))
                drafts = conn.settings.imap.drafts_folder or _special(client, b"\\Drafts")
                out.append(Check("email:drafts", "ok" if drafts else "warn",
                                 f"Drafts folder: {drafts}." if drafts else "No Drafts folder was found.",
                                 "" if drafts else "draft_unsupported",
                                 "" if drafts else "Set imap.drafts_folder, or use email.send approve."))
            finally:
                _logout(client)
        except ChannelError:
            raise
        except Exception as exc:  # noqa: BLE001
            out.append(Check("email:imap", "fail", f"IMAP sign-in failed ({type(exc).__name__}).",
                             "credential_rejected", "Check the host, port, TLS mode, username and password secret."))
        if conn.settings.smtp is None:
            out.append(Check("email:smtp", "warn", "No SMTP server: replies cannot be sent.", "smtp_missing",
                             "Add smtp settings, or keep email.send off or draft."))
            return out
        try:
            client = _smtp_connect(conn)
            client.noop()
            client.quit()
            out.append(Check("email:smtp", "ok", "Signed in to SMTP (nothing was sent)."))
        except (OSError, smtplib.SMTPException) as exc:
            out.append(Check("email:smtp", "fail", f"SMTP sign-in failed ({type(exc).__name__}).",
                             "credential_rejected", "Check the SMTP host, port, TLS mode and password secret."))
        return out


def _special(client, flag: bytes) -> str | None:
    try:
        for flags, _, name in client.list_folders():
            if flag in flags or flag.decode() in [f.decode() if isinstance(f, bytes) else f for f in flags]:
                return name
    except Exception:  # noqa: BLE001
        return None
    return None


def _logout(client) -> None:
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        pass


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _date(iso: str | None):
    try:
        return datetime.fromisoformat(iso).date() - timedelta(days=1)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).date() - timedelta(days=2)


# -- Gmail ---------------------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _Api:
    base_env = ""
    base_default = ""

    def base(self) -> str:
        return (os.environ.get(self.base_env) or self.base_default).rstrip("/")

    def call(self, conn, method: str, path: str, *, token: str | None = None, **kw):
        from . import oauth

        token = token or oauth.access_token(conn)
        try:
            resp = http().request(method, f"{self.base()}{path}", headers={"Authorization": f"Bearer {token}",
                                                                          **kw.pop("headers", {})}, **kw)
        except Exception as exc:  # noqa: BLE001 - the request may or may not have arrived
            exc.hubzoid_sent = method != "GET"
            raise
        return resp

    def send_call(self, conn, method: str, path: str, **kw):
        """A write whose outcome must be classified honestly."""
        import httpx

        try:
            resp = self.call(conn, method, path, **kw)
        except ChannelError as exc:
            raise _not_sent(f"{exc.code}: {exc.message}") from None
        except httpx.ConnectError:
            raise _not_sent("Could not reach the mail provider.") from None
        if resp.status_code == 429:
            raise _not_sent("The mail provider asked to slow down; nothing was sent.")
        if resp.status_code in (401, 403):
            raise _rejected(f"blocked_credentials: the mail provider refused the token ({resp.status_code}).")
        if 400 <= resp.status_code < 500:
            raise _rejected(f"The mail provider refused the message ({resp.status_code}).")
        if resp.status_code >= 500:
            raise RuntimeError(f"The mail provider answered {resp.status_code}; the outcome is unknown.")
        return resp


class Gmail(_Api):
    name = "gmail"
    base_env, base_default = "HUBZOID_GMAIL_API_URL", "https://gmail.googleapis.com"

    def fetch_new(self, conn, cursor: dict | None):
        if cursor is None:
            prof = self.call(conn, "GET", "/gmail/v1/users/me/profile")
            prof.raise_for_status()
            return [], {"history_id": str(prof.json()["historyId"])}
        ids, page, newest = [], None, cursor.get("history_id")
        for _ in range(10):
            params = {"startHistoryId": cursor["history_id"], "historyTypes": "messageAdded",
                      "labelId": conn.settings.gmail.label_ids[0], "maxResults": 100}
            if page:
                params["pageToken"] = page
            resp = self.call(conn, "GET", "/gmail/v1/users/me/history", params=params)
            if resp.status_code == 404:
                # The history is too old. Read the last two days again; admission
                # recognises each message already seen.
                log.warning("channel %s: Gmail history expired; re-reading two days", conn.id)
                lst = self.call(conn, "GET", "/gmail/v1/users/me/messages",
                                params={"q": "newer_than:2d", "labelIds": conn.settings.gmail.label_ids,
                                        "maxResults": BATCH})
                lst.raise_for_status()
                ids = [m["id"] for m in lst.json().get("messages") or []]
                prof = self.call(conn, "GET", "/gmail/v1/users/me/profile")
                newest = str(prof.json()["historyId"])
                break
            resp.raise_for_status()
            data = resp.json()
            newest = str(data.get("historyId") or newest)
            for h in data.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    m = added.get("message") or {}
                    if set(conn.settings.gmail.label_ids) & set(m.get("labelIds") or []) and m["id"] not in ids:
                        ids.append(m["id"])
            page = data.get("nextPageToken")
            if not page:
                break
        out = []
        for mid in ids[:BATCH]:
            resp = self.call(conn, "GET", f"/gmail/v1/users/me/messages/{mid}", params={"format": "raw"})
            if resp.status_code == 404:
                continue                      # deleted meanwhile
            resp.raise_for_status()
            data = resp.json()
            raw = _unb64url(data.get("raw") or "")
            out.append({"id": f"gmail:{mid}", "raw": raw if len(raw) <= MAX_RAW else b"",
                        "too_large": len(raw) > MAX_RAW, "thread_id": data.get("threadId")})
        if len(ids) > BATCH:
            # Keep the cursor where it was; the rest arrive on the next pass.
            return out, cursor
        return out, {"history_id": newest}

    def send(self, conn, raw: bytes, *, to: str, thread_id=None) -> str:
        body = {"raw": _b64url(raw)}
        if thread_id:
            body["threadId"] = thread_id
        return self.send_call(conn, "POST", "/gmail/v1/users/me/messages/send", json=body).json().get("id", "")

    def save_draft(self, conn, raw: bytes, *, message_id: str, thread_id=None) -> str:
        msg = {"raw": _b64url(raw)}
        if thread_id:
            msg["threadId"] = thread_id
        draft = self.send_call(conn, "POST", "/gmail/v1/users/me/drafts", json={"message": msg}).json()
        back = self.call(conn, "GET", f"/gmail/v1/users/me/drafts/{draft['id']}", params={"format": "minimal"})
        if back.status_code != 200:
            raise RuntimeError("the draft was saved but could not be read back")
        return f"gmail-draft:{draft['id']}"

    def find_sent(self, conn, message_id: str):
        resp = self.call(conn, "GET", "/gmail/v1/users/me/messages", params={"q": f"rfc822msgid:{message_id}",
                                                                              "maxResults": 5})
        if resp.status_code != 200:
            return None
        found = resp.json().get("messages") or []
        return found[0]["id"] if found else False

    def whoami(self, token: str):
        resp = self.call(None, "GET", "/gmail/v1/users/me/profile", token=token)
        if resp.status_code != 200:
            raise ChannelError("oauth_failed", "Gmail did not say which mailbox consented.", status=502)
        return resp.json().get("emailAddress")

    def doctor(self, conn) -> list[Check]:
        from . import oauth

        out = _override_checks()
        token = oauth.current_token(conn)
        if token is None:
            return out + [Check("email:gmail", "warn", "The mailbox is not connected, or its token has expired (it "
                                "renews on the next poll; doctor does not renew it).", "auth_required",
                                "Run hubzoid channels email connect, or wait for the next poll.")]
        prof = self.call(conn, "GET", "/gmail/v1/users/me/profile", token=token)
        if prof.status_code != 200:
            return out + [Check("email:gmail", "fail", f"Gmail refused the token ({prof.status_code}).",
                                "credential_rejected", "Connect the mailbox again.")]
        who = (prof.json().get("emailAddress") or "").lower()
        ok = who == conn.settings.mailbox.lower()
        out.append(Check("email:gmail", "ok" if ok else "fail", f"Connected to {who}.", "" if ok else
                         "mailbox_mismatch", "" if ok else "Connect the configured mailbox."))
        return out


# -- Microsoft 365 -----------------------------------------------------------------------------

class Graph(_Api):
    name = "microsoft365"
    base_env, base_default = "HUBZOID_GRAPH_API_URL", "https://graph.microsoft.com/v1.0"

    def _get_url(self, conn, url: str):
        from . import oauth

        return http().get(url, headers={"Authorization": f"Bearer {oauth.access_token(conn)}",
                                        "Prefer": "odata.maxpagesize=50"})

    def fetch_new(self, conn, cursor: dict | None):
        if cursor is None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{self.base()}/me/mailFolders/inbox/messages/delta?$select=id&$filter=receivedDateTime+ge+{now}"
        else:
            url = cursor["delta_link"]
        ids, delta = [], None
        for _ in range(50):
            resp = self._get_url(conn, url)
            if resp.status_code == 410 and cursor is not None:
                log.warning("channel %s: Graph delta expired; starting again from now", conn.id)
                return self.fetch_new(conn, None)
            resp.raise_for_status()
            data = resp.json()
            if cursor is not None:
                ids += [m["id"] for m in data.get("value") or [] if "@removed" not in m]
            if data.get("@odata.nextLink"):
                url = data["@odata.nextLink"]
                continue
            delta = data.get("@odata.deltaLink")
            break
        out = []
        for mid in ids[:BATCH]:
            resp = self.call(conn, "GET", f"/me/messages/{mid}/$value")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            raw = resp.content
            meta = self.call(conn, "GET", f"/me/messages/{mid}", params={"$select": "conversationId"})
            out.append({"id": f"graph:{mid}", "raw": raw if len(raw) <= MAX_RAW else b"",
                        "too_large": len(raw) > MAX_RAW,
                        "thread_id": meta.json().get("conversationId") if meta.status_code == 200 else None})
        if len(ids) > BATCH or not delta:
            return out, cursor if cursor is not None else {"delta_link": delta}
        return out, {"delta_link": delta}

    def send(self, conn, raw: bytes, *, to: str, thread_id=None) -> str:
        self.send_call(conn, "POST", "/me/sendMail", content=base64.b64encode(raw),
                       headers={"Content-Type": "text/plain"})
        return "graph"

    def save_draft(self, conn, raw: bytes, *, message_id: str, thread_id=None) -> str:
        draft = self.send_call(conn, "POST", "/me/messages", content=base64.b64encode(raw),
                               headers={"Content-Type": "text/plain"}).json()
        back = self.call(conn, "GET", f"/me/messages/{draft['id']}", params={"$select": "isDraft"})
        if back.status_code != 200 or not back.json().get("isDraft"):
            raise RuntimeError("the draft was saved but could not be read back")
        return f"graph-draft:{draft['id']}"

    def find_sent(self, conn, message_id: str):
        quoted = message_id.replace("'", "''")
        resp = self.call(conn, "GET", "/me/mailFolders/sentitems/messages",
                         params={"$filter": f"internetMessageId eq '{quoted}'", "$select": "id"})
        if resp.status_code != 200:
            return None
        found = resp.json().get("value") or []
        return found[0]["id"] if found else False

    def whoami(self, token: str):
        resp = self.call(None, "GET", "/me", token=token, params={"$select": "mail,userPrincipalName"})
        if resp.status_code != 200:
            raise ChannelError("oauth_failed", "Microsoft did not say which mailbox consented.", status=502)
        data = resp.json()
        return data.get("mail") or data.get("userPrincipalName")

    def doctor(self, conn) -> list[Check]:
        from . import oauth

        out = _override_checks()
        token = oauth.current_token(conn)
        if token is None:
            return out + [Check("email:graph", "warn", "The mailbox is not connected, or its token has expired (it "
                                "renews on the next poll; doctor does not renew it).", "auth_required",
                                "Run hubzoid channels email connect, or wait for the next poll.")]
        me = self.call(conn, "GET", "/me", token=token, params={"$select": "mail,userPrincipalName"})
        if me.status_code != 200:
            return out + [Check("email:graph", "fail", f"Microsoft refused the token ({me.status_code}).",
                                "credential_rejected", "Connect the mailbox again.")]
        who = (me.json().get("mail") or me.json().get("userPrincipalName") or "").lower()
        ok = who == conn.settings.mailbox.lower()
        out.append(Check("email:graph", "ok" if ok else "fail", f"Connected to {who}.", "" if ok else
                         "mailbox_mismatch", "" if ok else "Connect the configured mailbox."))
        return out
