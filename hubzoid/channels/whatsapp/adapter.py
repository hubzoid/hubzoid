"""WhatsApp: a company's own number on the Meta Cloud API as a source of
internal work.

Meta's signed webhook (``X-Hub-Signature-256`` over the raw body with the app
secret) is checked, the delivery is committed, and only then does Meta get
its 200. Each message goes to the intake service: it waits in quarantine for
a person (``unknown_sender: quarantine``, the default) or is closed with its
evidence (``reject``). No model sees a message before a person releases it,
and nothing is ever sent on its own.

Replies are ``customer_reply.send``. WhatsApp has no draft, so the hub's mode
for this channel is ``off`` or ``approve``: the work's requester approves each
exact message. A free-form message is only allowed within 24 hours of the
sender's latest message (Meta's customer service window); outside it the
reply is held, never sent as anything else. Delivery receipts are recorded
and shown with the reply. Media is fetched from Meta's own hosts only, with
no redirects, into the hub's file service.

Builds on ``hubzoid/whatsapp`` (signature check and payload parsing) used by
the older inbound harness, which stays for hubs without a channel file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import ChannelError
from ..models import Challenge, ExternalActor, InboundEvent, Origin, Receipt, Verified
from ..protocol import Adapter, Check, fail

log = logging.getLogger("hubzoid.channels.whatsapp")

GRAPH_VERSIONS = ("v22.0", "v23.0", "v24.0", "v25.0")
MEDIA_HOSTS = ("lookaside.fbsbx.com", "lookaside.facebook.com")
WINDOW_SECONDS = 24 * 3600
FUTURE_SKEW = 300
RECEIPT_ORDER = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


class Media(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_count: int = Field(default=10, ge=0, le=10)
    allowed_types: list[str] = Field(default_factory=lambda: ["application/pdf", "image/jpeg", "image/png"],
                                     max_length=50)


class Replies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mark_read: bool = False
    outside_window: Literal["hold"] = "hold"


class WhatsAppConfig(BaseModel):
    """`provider_config` for `provider: whatsapp_cloud`."""

    model_config = ConfigDict(extra="forbid")

    waba_id: str = Field(pattern=r"^\d{5,32}$")
    phone_number_id: str = Field(pattern=r"^\d{5,32}$")
    graph_version: str = "v22.0"
    app_secret_ref: str
    verify_token_ref: str
    access_token_ref: str
    unknown_sender: Literal["quarantine", "reject"] = "quarantine"
    media: Media = Field(default_factory=Media)
    replies: Replies = Field(default_factory=Replies)

    @field_validator("graph_version")
    @classmethod
    def _version(cls, v):
        if v not in GRAPH_VERSIONS:
            raise ValueError(f"graph_version is one of {', '.join(GRAPH_VERSIONS)} (tested with this release)")
        return v


def api_url() -> str:
    """Meta's Graph API. The override is for a local provider sandbox only."""
    return (os.environ.get("HUBZOID_WHATSAPP_API_URL") or "https://graph.facebook.com").rstrip("/")


def _http():
    from ..email import transports

    return transports.http()


class WhatsAppAdapter(Adapter):
    provider = "whatsapp_cloud"
    title = "WhatsApp"
    config_model = WhatsAppConfig
    webhook = True
    customer_facing = True
    send_action = "customer_reply.send"
    recovery = "escalate"
    part_chars = 4000
    max_parts = 4
    body_limit = 1024 * 1024
    secret_keys = ("app_secret_ref", "verify_token_ref", "access_token_ref")

    def check(self, config, settings: WhatsAppConfig):
        out = []
        if config.agent:
            out.append(("/agent", "WhatsApp is intake: set workflow (the work that starts for each released "
                        "message), not agent"))
        if config.run_as != "workflow":
            out.append(("/run_as", "WhatsApp senders are outside the organization: run_as is workflow"))
        if config.allowed_groups:
            out.append(("/allowed_groups", "WhatsApp senders are not hub members: leave allowed_groups empty"))
        return out

    def workspace(self, conn) -> str:
        return conn.settings.phone_number_id

    def allowed_types(self, conn) -> list[str]:
        return [t.lower() for t in conn.settings.media.allowed_types]

    def attachment_limits(self, conn):
        m = conn.settings.media
        return m.max_count, m.max_bytes, m.max_bytes * max(1, m.max_count)

    # -- intake ------------------------------------------------------------------------
    def verify(self, conn, raw, headers, query):
        from ...whatsapp.verify import verify_challenge, verify_signature

        if query.get("hub.mode"):
            challenge = verify_challenge(query, conn.secret("verify_token_ref"))
            if challenge is None:
                raise fail(403, "signature_invalid", "The verify token does not match.")
            return Challenge(200, challenge, media_type="text/plain")
        if not verify_signature(raw, headers.get("x-hub-signature-256"), conn.secret("app_secret_ref")):
            raise fail(401, "signature_invalid", "The signature does not match.")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise fail(400, "invalid_payload", "The body is not JSON.") from None
        # Meta sends no delivery id: the same bytes are the same delivery.
        return Verified(delivery_id="wa:" + hashlib.sha256(raw).hexdigest()[:40],
                        workspace_id=conn.settings.phone_number_id, method="meta_hmac_sha256", body=raw,
                        payload=payload)

    def parse(self, conn, verified):
        cfg: WhatsAppConfig = conn.settings
        out: list[InboundEvent] = []
        receipts: dict[str, dict] = {}
        windows: dict[str, int] = {}
        for entry in (verified.payload or {}).get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                value = (change or {}).get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict):
                    continue
                meta = value.get("metadata") or {}
                ours = str(meta.get("phone_number_id") or "") == cfg.phone_number_id and \
                    str(entry.get("id") or cfg.waba_id) == cfg.waba_id
                names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "")
                         for c in value.get("contacts") or [] if isinstance(c, dict)}
                for msg in value.get("messages") or []:
                    ev = self._message(conn, msg, names)
                    if ev is None:
                        continue
                    if not ours:
                        ev.ignore_reason = "phone_number_mismatch"
                    elif ev.data["trust"]["reason"] != "future_timestamp":
                        key = ev.origin.thread_key       # only a real, current message opens the window
                        windows[key] = max(windows.get(key, 0), int(ev.data.get("timestamp") or 0))
                    out.append(ev)
                for st in value.get("statuses") or []:
                    if ours and isinstance(st, dict) and st.get("id") and st.get("status") in RECEIPT_ORDER:
                        r = {"status": st["status"], "at": int(st.get("timestamp") or 0),
                             "error": ((st.get("errors") or [{}])[0] or {}).get("code")}
                        _merge_receipt(receipts, st["id"], r)
                        out.append(InboundEvent(
                            semantic_key=f"receipt:{st['id']}:{st['status']}", kind="receipt",
                            origin=self._origin(cfg, str(st.get("recipient_id") or "")), actor=None, trigger=False,
                            message_id=st["id"], data={"receipt": r}, ignore_reason="delivery_receipt"))
        if receipts:
            record_receipts(conn, receipts)
        if windows:
            record_windows(conn, windows)
        return out

    def _origin(self, cfg: WhatsAppConfig, wa_id: str) -> Origin:
        digits = re.sub(r"\D", "", wa_id or "")
        return Origin("whatsapp_cloud", cfg.phone_number_id, f"wa:{cfg.phone_number_id}:{digits}",
                      {"to": digits, "phone_number_id": cfg.phone_number_id})

    def _message(self, conn, msg, names) -> InboundEvent | None:
        from ...whatsapp.parse import _extract_caption, _extract_text

        cfg: WhatsAppConfig = conn.settings
        if not isinstance(msg, dict) or not msg.get("id") or not msg.get("from"):
            return None
        wa_id = re.sub(r"\D", "", str(msg["from"]))
        files = []
        for mtype in ("image", "document", "audio", "video", "voice", "sticker"):
            obj = msg.get(mtype) if isinstance(msg.get(mtype), dict) else {}
            if obj.get("id"):
                mime_type = (obj.get("mime_type") or "application/octet-stream").split(";")[0].strip()
                name = re.sub(r"[^A-Za-z0-9._ -]", "_", obj.get("filename") or f"{mtype}-{obj['id']}")[:200]
                files.append({"part": str(obj["id"]), "media_id": str(obj["id"]), "name": name,
                              "mimetype": mime_type, "size": None})
        text = _extract_text(msg) or _extract_caption(msg) or ""
        if msg.get("type") == "interactive":
            # The button's id is what it means; its title is display text.
            inter = msg.get("interactive") or {}
            reply = inter.get("button_reply") or inter.get("list_reply") or {}
            text = f"{reply.get('title') or ''} [{reply.get('id') or ''}]".strip()
        ts = int(msg.get("timestamp") or 0)
        trust = {"decision": "rejected" if cfg.unknown_sender == "reject" else "quarantined",
                 "reason": "unknown_sender", "evidence": {"signed": True}}
        if ts > time.time() + FUTURE_SKEW:
            trust = {"decision": "quarantined", "reason": "future_timestamp", "evidence": {"timestamp": ts}}
        ev = InboundEvent(
            semantic_key=f"wa:{msg['id']}", kind="message", origin=self._origin(cfg, wa_id),
            actor=ExternalActor("+" + wa_id, (names.get(msg["from"]) or "")[:200], False, {"signed": True}),
            text=text[:16000], trigger=True, message_id=str(msg["id"]), files=files[: cfg.media.max_count + 1],
            data={"trust": trust, "subject": "", "timestamp": ts, "message_type": msg.get("type"),
                  "provider_data": {"message_type": msg.get("type"), "provider_timestamp": ts,
                                    "phone_number_id": cfg.phone_number_id, "waba_id": cfg.waba_id}})
        if not text and not files:
            # Kept for review like any other message, with what it was.
            ev.text = f"[A {msg.get('type') or 'unknown'} message that Hubzoid cannot show]"
            ev.data["trust"] = {"decision": trust["decision"], "reason": "unsupported_message",
                                "evidence": {"message_type": msg.get("type")}}
        return ev

    def ack_response(self, admitted):
        return 200, {"received": True}

    def route(self, hub_dir, conn, row, thread, event):
        from ...intake import service as intake
        from ..router import _finish

        item = intake.capture(conn, row, thread, event)
        if conn.settings.replies.mark_read:
            try:
                _post(conn, {"messaging_product": "whatsapp", "status": "read", "message_id": event.message_id})
            except Exception:  # noqa: BLE001 - blue ticks are a courtesy
                pass
        if item["state"] == "dispatched":
            _finish(hub_dir, row["id"], "routed", None, target="work_item", work_item_id=item["work_item_id"],
                    intake_id=item["id"])
        else:
            _finish(hub_dir, row["id"], "routed", None, target="intake", intake_id=item["id"], replied="intake")
        return "routed", None

    def fetch_file(self, conn, event, ref):
        """Meta's two steps: the media id gives a short-lived URL on Meta's own
        host, then the bytes. The token goes only to those hosts."""
        cfg: WhatsAppConfig = conn.settings
        token = conn.secret("access_token_ref")
        auth = {"Authorization": f"Bearer {token}"}
        meta = _http().get(f"{api_url()}/{cfg.graph_version}/{ref['media_id']}",
                           params={"phone_number_id": cfg.phone_number_id}, headers=auth)
        if meta.status_code != 200:
            raise ChannelError("attachment_unavailable", f"Meta answered {meta.status_code} for the media.",
                               status=422 if meta.status_code < 500 else 503)
        url = (meta.json() or {}).get("url") or ""
        p = urlparse(url)
        allowed = set(MEDIA_HOSTS) | ({urlparse(api_url()).hostname} if os.environ.get("HUBZOID_WHATSAPP_API_URL")
                                      else set())
        if p.scheme not in ("https",) + (("http",) if os.environ.get("HUBZOID_WHATSAPP_API_URL") else ()) \
                or p.hostname not in allowed or p.username or p.password:
            raise ChannelError("attachment_blocked", "The media is not on Meta's media host.", status=422)
        data = bytearray()
        with _http().stream("GET", url, headers=auth) as resp:
            if resp.status_code != 200:
                raise ChannelError("attachment_unavailable", f"Meta answered {resp.status_code}.", status=422)
            for chunk in resp.iter_bytes():
                data.extend(chunk)
                if len(data) > cfg.media.max_bytes:
                    raise ChannelError("attachment_too_large", "The media is larger than this channel accepts.",
                                       status=413)
        mime_type = (meta.json() or {}).get("mime_type") or ref.get("mimetype") or "application/octet-stream"
        return ref.get("name") or "media", mime_type.split(";")[0].strip(), bytes(data)

    # -- replies ------------------------------------------------------------------------
    def reply(self, conn, origin: Origin, outbound) -> Receipt:
        from ...work_items.errors import NotSent

        last = last_inbound_at(conn, origin)
        if not last or time.time() > last + WINDOW_SECONDS:
            raise NotSent("window_closed: the sender's last message is more than 24 hours old, so WhatsApp allows "
                          "no free-form reply. It is held; retry it in Operations after they write again.")
        data = _post(conn, {"messaging_product": "whatsapp", "recipient_type": "individual",
                            "to": origin.coordinates["to"], "type": "text",
                            "text": {"preview_url": False, "body": outbound.text}})
        mid = ((data.get("messages") or [{}])[0] or {}).get("id") or ""
        return Receipt(mid, {"provider_message_id": mid})

    def find(self, conn, origin: Origin, marker: str):
        return None     # Meta cannot look a message up by our reference: a person resolves it

    def context(self, conn, event, *, first):
        return ""

    def thread_title(self, thread):
        c = (thread.get("origin_json") or {}).get("coordinates") or {}
        return f"WhatsApp: +{c.get('to') or ''}"

    # -- setup ---------------------------------------------------------------------------
    def doctor(self, conn, inputs) -> list[Check]:
        cfg: WhatsAppConfig = conn.settings
        out: list[Check] = []
        if os.environ.get("HUBZOID_WHATSAPP_API_URL"):
            out.append(Check("whatsapp:override", "warn", "HUBZOID_WHATSAPP_API_URL is set. Use it only with a "
                             "local sandbox.", "provider_override", "Unset it for a real number."))
        try:
            resp = _http().get(f"{api_url()}/{cfg.graph_version}/{cfg.phone_number_id}",
                               params={"fields": "display_phone_number,verified_name"},
                               headers={"Authorization": f"Bearer {conn.secret('access_token_ref')}"})
        except ChannelError:
            return out
        except Exception as exc:  # noqa: BLE001
            return out + [Check("whatsapp:number", "fail", f"Could not reach Meta ({type(exc).__name__}).",
                                "provider_unavailable", "Check the network, then run doctor again.")]
        if resp.status_code != 200:
            out.append(Check("whatsapp:number", "fail", f"Meta refused the access token for this number "
                             f"({resp.status_code}).", "credential_rejected",
                             "Use a system user token with whatsapp_business_messaging for this number."))
        else:
            d = resp.json() or {}
            out.append(Check("whatsapp:number", "ok", f"{d.get('verified_name') or 'The number'} "
                             f"({d.get('display_phone_number') or cfg.phone_number_id}) answers with this token."))
        out.extend(_mode_checks(conn))
        return out

    def routes(self, router, hub_dir) -> None:
        return None


def _post(conn, payload: dict) -> dict:
    """One send to Meta, with honest outcomes."""
    import httpx

    from ...work_items.errors import NotSent, Rejected

    cfg: WhatsAppConfig = conn.settings
    try:
        resp = _http().post(f"{api_url()}/{cfg.graph_version}/{cfg.phone_number_id}/messages", json=payload,
                            headers={"Authorization": f"Bearer {conn.secret('access_token_ref')}"})
    except httpx.ConnectError:
        raise NotSent("Could not reach Meta; nothing was sent.") from None
    if resp.status_code == 429:
        raise NotSent("Meta asked to slow down; nothing was sent.")
    if resp.status_code in (401, 403):
        raise Rejected(f"blocked_credentials: Meta refused the token ({resp.status_code}).")
    if 400 <= resp.status_code < 500:
        code = ((resp.json() if resp.content else {}).get("error") or {}).get("code")
        raise Rejected(f"Meta refused the message ({resp.status_code}{', code ' + str(code) if code else ''}).")
    if resp.status_code >= 500:
        raise RuntimeError(f"Meta answered {resp.status_code}; the outcome is unknown.")
    return resp.json() if resp.content else {}


def last_inbound_at(conn, origin: Origin) -> int:
    """The provider timestamp of the sender's newest message in this thread,
    as recorded when it arrived. It only ever moves forward: a delayed replay
    or a future-dated message never extends the window."""
    from .. import store

    row = store.get_connection(conn.hub_dir, conn.hub, conn.id) or {}
    return int(((row.get("state_json") or {}).get("wa_window") or {}).get(origin.thread_key) or 0)


def record_windows(conn, windows: dict) -> None:
    from .. import store

    def merge(state: dict):
        known = dict(state.get("wa_window") or {})
        for key, ts in windows.items():
            known[key] = max(int(known.get(key) or 0), int(ts))
        if len(known) > 5000:
            known = dict(sorted(known.items(), key=lambda kv: kv[1])[-5000:])
        return {"wa_window": known}, None

    store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, merge)


def _merge_receipt(known: dict, mid: str, r: dict) -> None:
    old = known.get(mid)
    if old is None or RECEIPT_ORDER[r["status"]] >= RECEIPT_ORDER.get(old["status"], 0):
        known[mid] = r


def record_receipts(conn, receipts: dict) -> None:
    """Keep the newest status of each sent message; a late `sent` never
    replaces `read`."""
    from .. import store

    def merge(state: dict):
        known = dict(state.get("delivery_receipts") or {})
        for mid, r in receipts.items():
            _merge_receipt(known, mid, r)
        if len(known) > 1000:
            known = dict(sorted(known.items(), key=lambda kv: kv[1].get("at") or 0)[-1000:])
        return {"delivery_receipts": known}, None

    store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, merge)


def receipt_for(conn, provider_message_id: str) -> dict | None:
    return ((conn.state or {}).get("delivery_receipts") or {}).get(provider_message_id)


def _mode_checks(conn) -> list[Check]:
    from ...work_items import config as wconfig

    from ...work_items.errors import WorkItemError

    try:
        mode = wconfig.mode_for(wconfig.load(conn.hub_dir), ["customer_reply.send"], conn.id)
    except WorkItemError as exc:
        return [Check("whatsapp:send_mode", "fail", exc.message, "invalid_config", "Fix work-items.yaml, then run "
                      "hubzoid work-items config apply.")]
    if mode == "draft":
        return [Check("whatsapp:send_mode", "fail", "customer_reply.send is draft for this channel, but WhatsApp has "
                      "no drafts.", "draft_unsupported", f"Set write_mode_overrides.{conn.id}.customer_reply.send to "
                      "off or approve in work-items.yaml.")]
    return [Check("whatsapp:send_mode", "ok", "Replies are off: nothing is sent to senders." if mode == "off" else
                  "Each reply waits for its requester to approve the exact message, inside the 24-hour window.")]
