"""Email: a shared mailbox as a source of internal work.

New mail is polled (IMAP read-only with ``BODY.PEEK``, Gmail's history, or
Microsoft Graph's delta), saved with its raw source, and handed to the
intake service: senders on the allowlist with an aligned DKIM signature start
the configured workflow; anyone else waits in quarantine for a person. No
model sees an unadmitted message and nothing is ever sent to a sender on its
own.

Replies are a workflow's ``hub.thread.post(..., actions="email.send")``. The
hub's write mode for ``email.send`` decides what happens: ``off`` refuses,
``draft`` saves a real draft in the mailbox (Gmail drafts, a Microsoft 365
draft, or an IMAP APPEND to the Drafts folder, each read back), ``approve``
waits for a person to approve the exact message in Hubzoid and then sends it
over SMTP, Gmail or Graph. The recipient is always the admitted sender, in
the same conversation (In-Reply-To and References), never a Reply-To header,
a cc or anything a model wrote. A send whose outcome is unknown is never sent
again on its own.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ChannelError
from ..models import Origin, Receipt
from ..protocol import Adapter, Check

log = logging.getLogger("hubzoid.channels.email")


class TLSHost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    tls: Literal["implicit", "starttls"]
    username: str = Field(min_length=1, max_length=320)
    password_ref: str


class ImapConfig(TLSHost):
    folder: str = Field(default="INBOX", min_length=1, max_length=255)
    drafts_folder: str | None = Field(default=None, max_length=255)


class GmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(min_length=1, max_length=255)
    client_secret_ref: str
    oauth_ref: str
    label_ids: list[str] = Field(default_factory=lambda: ["INBOX"], min_length=1, max_length=20)


class Microsoft365Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=255)
    client_secret_ref: str
    oauth_ref: str


class SenderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: list[str] = Field(default_factory=list, max_length=1000)
    authentication: Literal["aligned_dkim", "manual"] = "aligned_dkim"
    unknown: Literal["quarantine"] = "quarantine"

    @field_validator("allowed")
    @classmethod
    def _addresses(cls, v):
        for a in v:
            if "*" in a or "@" not in a or len(a) > 320:
                raise ValueError(f"{a!r} is not one exact address (no wildcards)")
        return v


class Attachments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_types: list[str] = Field(default_factory=lambda: ["application/pdf", "text/csv", "text/plain",
                                                              "image/png", "image/jpeg"], max_length=50)
    max_count: int = Field(default=10, ge=0, le=10)
    max_each_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=10 * 1024 * 1024)
    max_total_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    require_all: Literal[True] = True


class EmailConfig(BaseModel):
    """`provider_config` for `provider: email`."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["imap", "gmail", "microsoft365"]
    mailbox: str = Field(min_length=3, max_length=320)
    poll_seconds: int = Field(default=60, ge=15, le=3600)
    start: Literal["new_only"] = "new_only"
    imap: ImapConfig | None = None
    smtp: TLSHost | None = None
    gmail: GmailConfig | None = None
    microsoft365: Microsoft365Config | None = None
    sender_policy: SenderPolicy = Field(default_factory=SenderPolicy)
    attachments: Attachments = Field(default_factory=Attachments)

    @model_validator(mode="after")
    def _transport(self):
        need = {"imap": self.imap, "gmail": self.gmail, "microsoft365": self.microsoft365}
        if need[self.transport] is None:
            raise ValueError(f"transport {self.transport} needs its {self.transport} settings")
        for name, value in need.items():
            if name != self.transport and value is not None:
                raise ValueError(f"{name} settings are only for transport: {name}")
        if self.transport != "imap" and self.smtp is not None:
            raise ValueError("smtp is only for transport: imap (Gmail and Microsoft 365 send through their API)")
        return self


class EmailAdapter(Adapter):
    provider = "email"
    title = "Email"
    config_model = EmailConfig
    webhook = False
    customer_facing = True
    send_action = "email.send"
    recovery = "escalate"
    part_chars = 200_000
    max_parts = 1
    secret_keys = ()

    def check(self, config, settings: EmailConfig):
        out = []
        if config.agent:
            out.append(("/agent", "Email is intake: set workflow (the work that starts for each message), not agent"))
        if config.run_as != "workflow":
            out.append(("/run_as", "Email senders are outside the organization: run_as is workflow"))
        if config.allowed_groups:
            out.append(("/allowed_groups", "Email senders are not hub members: leave allowed_groups empty and use "
                        "provider_config.sender_policy"))
        return out

    def workspace(self, conn) -> str:
        return conn.settings.mailbox.lower()

    def allowed_types(self, conn) -> list[str]:
        return [t.lower() for t in conn.settings.attachments.allowed_types]

    def attachment_limits(self, conn):
        a = conn.settings.attachments
        return a.max_count, a.max_each_bytes, a.max_total_bytes

    # -- intake -------------------------------------------------------------------
    def verify(self, conn, raw, headers, query):
        raise ChannelError("not_found", "Email arrives by polling the mailbox, not by webhook.", status=404)

    def parse(self, conn, verified):
        return list(verified.payload or [])

    def route(self, hub_dir, conn, row, thread, event):
        from ...intake import service as intake

        item = intake.capture(conn, row, thread, event)
        from ..router import _finish

        if item["state"] == "dispatched":
            _finish(hub_dir, row["id"], "routed", None, target="work_item", work_item_id=item["work_item_id"],
                    intake_id=item["id"], account=f"workflow:{conn.config.workflow}")
            return "routed", None
        _finish(hub_dir, row["id"], "routed", None, target="intake", intake_id=item["id"], replied="intake")
        return "routed", None

    def fetch_file(self, conn, event, ref):
        from ...intake import service as intake
        from . import mime

        source = event.data.get("source_file_id")
        if not source:
            raise ChannelError("attachment_unavailable", "The message source was not kept.", status=422)
        _, raw = intake.files_for(conn.hub_dir).content(intake.system_actor(conn.id), source)
        try:
            return mime.attachment_bytes(raw, ref["part"])
        except KeyError:
            raise ChannelError("attachment_unavailable", "The attachment is not in the message.", status=422) from None

    # -- replies ----------------------------------------------------------------------
    def _reply_message(self, conn, origin: Origin, outbound) -> tuple[bytes, str, dict]:
        from . import mime

        c = origin.coordinates
        domain = conn.settings.mailbox.rpartition("@")[2] or "hubzoid.local"
        msgid = f"<{outbound.marker}@{domain}>"
        last = _last_inbound(conn, origin)
        raw = mime.build_reply(mailbox=conn.settings.mailbox, to=c["reply_to"], subject=c.get("subject") or "",
                               body=outbound.text, in_reply_to=last.get("message_id") or c.get("root_message_id"),
                               references=last.get("references") or [], message_id=msgid)
        return raw, msgid, c

    def reply(self, conn, origin: Origin, outbound) -> Receipt:
        from . import transports

        raw, msgid, c = self._reply_message(conn, origin, outbound)
        sent = transports.for_conn(conn).send(conn, raw, to=c["reply_to"], thread_id=c.get("provider_thread_id"))
        return Receipt(msgid, {"provider_id": sent})

    def draft(self, conn, origin: Origin, outbound) -> Receipt:
        """A real draft in the mailbox, read back. Never a local preview."""
        from . import transports

        if not (conn.state or {}).get("email_draft_verified"):
            from ...work_items.errors import NotSent

            raise NotSent("draft_unsupported: drafts are not verified for this mailbox yet. Run hubzoid channels "
                          "email rehearse-draft, or set email.send to approve.")
        raw, msgid, c = self._reply_message(conn, origin, outbound)
        draft_id = transports.for_conn(conn).save_draft(conn, raw, message_id=msgid,
                                                        thread_id=c.get("provider_thread_id"))
        return Receipt(draft_id, {"message_id": msgid, "draft": True})

    def find(self, conn, origin: Origin, marker: str):
        from . import transports

        domain = conn.settings.mailbox.rpartition("@")[2] or "hubzoid.local"
        try:
            found = transports.for_conn(conn).find_sent(conn, f"<{marker}@{domain}>")
        except Exception:  # noqa: BLE001 - cannot look
            return None
        if found is None:
            return None
        return Receipt(found) if found else False

    def context(self, conn, event, *, first):
        return ""

    def thread_title(self, thread):
        c = (thread.get("origin_json") or {}).get("coordinates") or {}
        return f"Email: {c.get('subject') or '(no subject)'}"

    # -- running ------------------------------------------------------------------------
    def start(self, conn, hub_dir):
        from .poller import Poller

        poller = Poller(conn)
        poller.start()
        return poller

    def doctor(self, conn, inputs) -> list[Check]:
        from . import transports

        out = transports.for_conn(conn).doctor(conn)
        out.extend(_mode_checks(conn))
        return out

    def routes(self, router, hub_dir) -> None:
        from .routes import mount

        mount(router, hub_dir)


def _last_inbound(conn, origin: Origin) -> dict:
    """The newest message in the thread that started work (admitted, or
    released by a person): what a reply answers. A later message still in
    review cannot change a reply's In-Reply-To or References."""
    from sqlalchemy import text

    from .. import admission, store

    with store.engine(conn.hub_dir).connect() as c:
        row = c.execute(text("SELECT d.* FROM hz_channel_deliveries d JOIN hz_channel_threads t ON t.id=d.thread_id "
                             "JOIN hz_intake_items i ON i.delivery_id=d.id AND i.state='dispatched' "
                             "WHERE t.hub=:h AND t.connection_id=:c AND t.thread_key=:k AND d.event_enc IS NOT NULL "
                             "ORDER BY d.created DESC LIMIT 1"),
                        {"h": conn.hub, "c": conn.id, "k": origin.thread_key}).mappings().first()
    if row is None:
        return {}
    ev = admission.event_of(conn.hub_dir, dict(row))
    return {"message_id": ev.data.get("message_id"), "references": ev.data.get("references") or []}


def _mode_checks(conn) -> list[Check]:
    from ...work_items import config as wconfig

    from ...work_items.errors import WorkItemError

    try:
        mode = wconfig.mode_for(wconfig.load(conn.hub_dir), ["email.send"], conn.id)
    except WorkItemError as exc:
        return [Check("email:send_mode", "fail", exc.message, "invalid_config", "Fix work-items.yaml, then run "
                      "hubzoid work-items config apply.")]
    verified = bool((conn.state or {}).get("email_draft_verified"))
    msg = {"off": "Replies are off: workflows cannot email senders.",
           "draft": "Replies are saved as drafts in the mailbox for a person to send." + (
               "" if verified else " Drafts are not verified for this mailbox yet."),
           "approve": "Each reply waits for a person to approve the exact message, then it is sent."}[mode]
    status = "warn" if mode == "draft" and not verified else "ok"
    return [Check("email:send_mode", status, f"email.send is {mode}. {msg}", "draft_unsupported" if status == "warn"
                  else "", "Run hubzoid channels email rehearse-draft, or set email.send to approve in "
                           "work-items.yaml." if status == "warn" else "")]

