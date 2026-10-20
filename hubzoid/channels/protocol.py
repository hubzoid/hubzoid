"""The provider boundary: what every channel adapter implements.

An adapter knows one provider's wire format and nothing about runs, queues or
identities. The channel core does the rest:

* ``verify(conn, raw, headers)`` checks the provider's signature on the exact
  received bytes, before anything is parsed. It returns a ``Verified`` delivery
  or a ``Challenge`` for a protocol handshake, and raises ``ChannelError`` with
  status 401 or 403 when the request is not authentic. Verification is never
  optional.
* ``parse(conn, verified)`` turns the delivery into ``InboundEvent`` rows.
  It reads only the verified payload and never calls the provider.
* ``reply(conn, origin, outbound)`` posts one rendered part to the stored
  origin and returns the provider's ``Receipt``. The origin always comes from
  the stored thread, never from message text or a model.
* ``find(conn, origin, marker)`` optionally looks for a part already posted
  with that marker, for recovery after an uncertain post. ``None`` means "not
  supported" (the post stays uncertain). An empty result means "not seen",
  which is never taken as proof that nothing was posted.
* ``doctor(conn)`` runs read-only checks and never posts.

The design follows Sim's separated verify, match, format and subscribe hooks
(``apps/sim/lib/webhooks/providers/types.ts:119-207``), with verification made
mandatory, and eve's channel boundary of routes, state and events
(``packages/eve/src/public/definitions/channel.ts:244-280``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from .errors import ChannelError
from .models import Challenge, InboundEvent, Origin, Outbound, Receipt, Verified

if TYPE_CHECKING:
    from .config import Connection


class EmptyProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Check:
    """One doctor line: `status` is ok, warn, fail or skip."""

    id: str
    status: str
    message: str
    code: str = ""
    recovery: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "status": self.status, "message": self.message, "code": self.code,
                "recovery": self.recovery}


class Adapter:
    """Base class. Subclasses set `provider`, `config_model` and the limits,
    and implement verify, parse and reply."""

    provider: ClassVar[str] = ""
    title: ClassVar[str] = ""
    config_model: ClassVar[type[BaseModel]] = EmptyProviderConfig
    #: the largest raw request body accepted before parsing
    body_limit: ClassVar[int] = 1024 * 1024
    #: the longest reply part, in Unicode characters, and the most parts per reply
    part_chars: ClassVar[int] = 3500
    max_parts: ClassVar[int] = 20
    #: how an uncertain post is recovered: "reconcile" when `find` works, else "escalate"
    recovery: ClassVar[str] = "escalate"
    #: names of the secret slots this provider uses, for doctor and the Console
    secret_keys: ClassVar[tuple[str, ...]] = ()
    #: True when the provider delivers by webhook (needs HUBZOID_PUBLIC_URL)
    webhook: ClassVar[bool] = True
    #: True when the people on the other side are outside the organization (an
    #: email sender, a WhatsApp contact). Nothing is posted to them on its own:
    #: no acknowledgement, no notice and no automatic final reply. Every reply
    #: is a workflow's `hub.thread.post` under the hub's write mode.
    customer_facing: ClassVar[bool] = False
    #: which replies can carry rating controls when the channel file says
    #: ``feedback: true`` (``hubzoid.channels.feedback``): "agent" (a chat
    #: answer), "workflow" (a workflow's final reply), or "" for none
    feedback_target: ClassVar[str] = ""

    def route(self, hub_dir, conn: "Connection", row: dict, thread: dict, event: InboundEvent):
        """Take over routing for this provider (intake adapters hand events to
        the intake service). Return (outcome, plan) like the core router, or
        NotImplemented to use the core's routing."""
        return NotImplemented

    # -- required -----------------------------------------------------------
    def verify(self, conn: "Connection", raw: bytes, headers: dict, query: dict) -> Verified | Challenge:
        raise NotImplementedError

    def parse(self, conn: "Connection", verified: Verified) -> list[InboundEvent]:
        raise NotImplementedError

    def reply(self, conn: "Connection", origin: Origin, outbound: Outbound) -> Receipt:
        raise NotImplementedError

    # -- optional -----------------------------------------------------------
    def find(self, conn: "Connection", origin: Origin, marker: str) -> Receipt | None | bool:
        """`Receipt` when found, False when looked and not found, None when
        this provider cannot look."""
        return None

    def doctor(self, conn: "Connection", inputs: dict) -> list[Check]:
        return []

    def render(self, text: str) -> str:
        """Make agent text safe for the provider (escape mentions, convert
        markup). The default is the text itself."""
        return text

    def validate(self, provider_config: dict, *, hub_dir, connection_id: str) -> BaseModel:
        return self.config_model.model_validate(provider_config)

    def routes(self, router, hub_dir) -> None:
        """Mount provider-specific routes (setup and OAuth callbacks) on the
        hub-scoped channels router."""

    def start(self, conn: "Connection", hub_dir) -> Any:
        """Start a long-lived transport (Socket Mode, a mailbox poller) for an
        enabled connection. Returns a handle with `stop()`, or None."""
        return None

    def check(self, config, settings) -> list[tuple[str, str]]:
        """Rules that need the whole file (for example "at least one
        repository when enabled"): (field pointer, message) pairs."""
        return []

    def authorize(self, conn: "Connection", event: InboundEvent) -> str | None:
        """A provider-side check before routing that may call the provider
        (for example a GitHub labeler's live permission). Returns a refusal
        reason, or None. Raise ChannelError with a 5xx status to try again
        later. The refused person sees nothing, so private state never leaks."""
        return None

    def context(self, conn: "Connection", event: InboundEvent, *, first: bool) -> str:
        """A short note put before the person's message: where it came from,
        and (when `first`, the person's first request in this thread) any
        earlier thread messages as context. Never instructions."""
        return ""

    def workspace(self, conn: "Connection") -> str | None:
        """The provider workspace this connection serves (a Slack team, a
        Linear organization), for account links. None when it varies."""
        return None

    def ack_response(self, admitted) -> tuple[int, Any]:
        """What the provider is told once a delivery is saved."""
        return 200, admitted.to_json()

    def link_hint(self, origin: Origin, actor) -> str:
        """What an administrator needs to link this account, for the notice an
        unlinked person sees and for doctor."""
        return f"{self.provider} account {actor.external_id if actor else ''}".strip()


def fail(status: int, code: str, message: str, **extra) -> ChannelError:
    return ChannelError(code, message, status=status, **extra)


__all__ = ["Adapter", "Check", "Challenge", "Verified", "InboundEvent", "Origin", "Outbound", "Receipt",
           "fail"]
