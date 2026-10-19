"""A small channel adapter for testing the shared contract (tests only).

Deliveries are JSON signed with HMAC-SHA256 of the raw body under the
connection's secret, in ``X-Fake-Signature``, with a delivery id in
``X-Fake-Delivery``:

    {"events": [{"id": "e1", "thread": "t1", "user": "U1", "text": "hi",
                 "trigger": true, "files": []}]}

Replies are kept in memory on the adapter, so a test can see exactly what was
posted, where, and how many times. `fail_next` makes the next post raise (the
outcome is then unknown), and `lose_next` makes the provider accept the post
but the call time out, which is the case that must never be resent blindly.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import BaseModel, ConfigDict

from hubzoid.channels.models import ExternalActor, InboundEvent, Origin, Receipt, Verified
from hubzoid.channels.protocol import Adapter, Check, fail


class FakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_ref: str
    workspace: str = "W1"
    part_chars: int | None = None


def sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class FakeAdapter(Adapter):
    provider = "fake"
    title = "Fake"
    config_model = FakeConfig
    secret_keys = ("secret_ref",)
    recovery = "reconcile"
    webhook = True

    def __init__(self):
        self.sent: list[dict] = []
        self.fail_next: Exception | None = None
        self.lose_next = False
        self.findable = True

    def verify(self, conn, raw, headers, query):
        sig = headers.get("x-fake-signature", "")
        delivery = headers.get("x-fake-delivery", "")
        if not sig or not delivery:
            raise fail(401, "signature_invalid", "Missing signature or delivery id.")
        if not hmac.compare_digest(sig, sign(conn.secret("secret_ref"), raw)):
            raise fail(401, "signature_invalid", "The signature does not match.")
        return Verified(delivery_id=delivery, workspace_id=conn.settings.workspace, method="hmac-sha256",
                        body=raw, payload=json.loads(raw))

    def parse(self, conn, verified):
        out = []
        for e in verified.payload.get("events", []):
            origin = Origin("fake", verified.workspace_id, f"fake:{e['thread']}", {"thread": e["thread"]},
                            url=f"https://fake.example/{e['thread']}")
            out.append(InboundEvent(semantic_key=f"msg:{e['id']}", kind=e.get("kind", "message"), origin=origin,
                                    actor=ExternalActor(e["user"], e.get("name", ""), bool(e.get("bot"))),
                                    text=e.get("text", ""), trigger=bool(e.get("trigger", True)),
                                    message_id=e["id"], files=e.get("files", []),
                                    data=dict(e.get("data", {}))))
        return out

    def reply(self, conn, origin, outbound):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        record = {"thread": origin.coordinates["thread"], "text": outbound.text, "kind": outbound.kind,
                  "marker": outbound.marker, "part": outbound.part, "parts": outbound.parts}
        self.sent.append(record)
        if self.lose_next:
            self.lose_next = False
            raise TimeoutError("the provider did not answer in time")
        return Receipt(f"m{len(self.sent)}", {"thread": record["thread"]})

    def find(self, conn, origin, marker):
        if not self.findable:
            return None
        for n, s in enumerate(self.sent, 1):
            if s["marker"] == marker and s["thread"] == origin.coordinates["thread"]:
                return Receipt(f"m{n}")
        return False

    def workspace(self, conn):
        return conn.settings.workspace

    def doctor(self, conn, inputs):
        return [Check("fake", "ok", "The fake provider answers.")]

    def context(self, conn, event, *, first):
        return f"(Fake thread {event.origin.coordinates['thread']})"


def delivery(*events, delivery_id="d1") -> tuple[bytes, str]:
    raw = json.dumps({"events": list(events)}).encode()
    return raw, delivery_id


def headers(secret: str, raw: bytes, delivery_id: str) -> dict:
    return {"x-fake-signature": sign(secret, raw), "x-fake-delivery": delivery_id}
