"""Channel shapes: the connection file, a verified delivery, a normalized event
and the origin a reply goes back to.

These are plain pydantic models and dataclasses with no runtime SDK imports,
so a hub folder means the same thing to every runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

#: run identities available today. HUB-88 adds `requester` and `user:<id>`.
RUN_AS = ("linked_requester", "workflow")


class ChannelConfig(BaseModel):
    """`channels/<id>.yaml`: the keys every provider shares. Everything a
    provider needs of its own lives under `provider_config`, which the
    provider's adapter validates."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    id: str
    provider: str
    enabled: bool = False
    agent: str | None = None
    workflow: str | None = None
    run_as: str | None = None
    allowed_groups: list[str] = Field(default_factory=list)
    queue_limit: int = Field(default=100, ge=1, le=1000)
    subscriptions: Literal["on_accept", "explicit"] = "on_accept"
    progress: bool = True
    #: rating controls on final replies (Slack agent answers, WhatsApp workflow replies)
    feedback: bool = False
    provider_config: dict = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("must be 1 to 64 lowercase letters, digits, '-' or '_', starting with a "
                             "letter or digit")
        return v

    @field_validator("allowed_groups")
    @classmethod
    def _groups(cls, v: list[str]) -> list[str]:
        for g in v:
            if not GROUP_RE.match(g):
                raise ValueError(f"{g!r} is not a group name")
        if len(set(v)) != len(v):
            raise ValueError("lists a group twice")
        return v

    @model_validator(mode="after")
    def _target(self):
        if bool(self.agent) == bool(self.workflow):
            raise ValueError("set exactly one of agent (a chat agent answers) or workflow (a workflow "
                             "starts for each request)")
        if self.run_as is None:
            self.run_as = "linked_requester" if self.agent else "workflow"
        if self.run_as not in RUN_AS:
            raise ValueError(f"run_as must be one of {', '.join(RUN_AS)}")
        if self.agent and self.run_as != "linked_requester":
            raise ValueError("an agent connection answers as the person who asked, so run_as is "
                             "linked_requester")
        return self

    @property
    def target(self) -> str:
        return f"agent:{self.agent}" if self.agent else f"workflow:{self.workflow}"


# -- what an adapter returns ---------------------------------------------------

@dataclass(frozen=True)
class Verified:
    """A delivery whose bytes the provider signed (or whose transport the
    provider authenticated). `delivery_id` is the provider's id for this
    delivery; `method` names how it was verified."""

    delivery_id: str
    workspace_id: str
    method: str
    body: bytes = b""
    payload: Any = None


@dataclass(frozen=True)
class Challenge:
    """A protocol handshake answered without starting work (Slack's URL
    verification, Meta's subscribe check)."""

    status: int = 200
    body: Any = None
    media_type: str = "application/json"


@dataclass(frozen=True)
class Origin:
    """Where replies go. Immutable once a thread exists. `coordinates` are the
    provider's own ids (channel and thread timestamp, repository id and issue
    number, agent session id). `url` is for people to click, never a request
    target."""

    provider: str
    workspace_id: str
    thread_key: str
    coordinates: dict
    url: str = ""

    def to_json(self) -> dict:
        return {"provider": self.provider, "workspace_id": self.workspace_id,
                "thread_key": self.thread_key, "coordinates": dict(self.coordinates), "url": self.url}

    @classmethod
    def from_json(cls, data: dict) -> "Origin":
        return cls(provider=data["provider"], workspace_id=data["workspace_id"],
                   thread_key=data["thread_key"], coordinates=dict(data.get("coordinates") or {}),
                   url=data.get("url") or "")


@dataclass(frozen=True)
class ExternalActor:
    """The provider's verified account for who sent an event. Names are for
    display only; `external_id` is what links resolve."""

    external_id: str
    display: str = ""
    is_bot: bool = False
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"external_id": self.external_id, "display": self.display, "is_bot": self.is_bot,
                "evidence": dict(self.evidence)}


@dataclass
class InboundEvent:
    """One normalized event from a verified delivery.

    `semantic_key` names the event itself (not the delivery), so the same
    message delivered twice, or as two event types, is admitted once.
    `trigger` says whether it asks for work (a mention, a delegation, a direct
    message). A non-trigger event in a subscribed thread continues the work;
    elsewhere it is recorded and ignored. `text` and `files` are untrusted data."""

    semantic_key: str
    kind: str
    origin: Origin
    actor: ExternalActor | None
    text: str = ""
    trigger: bool = True
    message_id: str = ""
    files: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    ignore_reason: str | None = None

    def to_json(self) -> dict:
        return {"semantic_key": self.semantic_key, "kind": self.kind, "origin": self.origin.to_json(),
                "actor": self.actor.to_json() if self.actor else None, "text": self.text,
                "trigger": self.trigger, "message_id": self.message_id, "files": list(self.files),
                "data": dict(self.data), "ignore_reason": self.ignore_reason}

    @classmethod
    def from_json(cls, data: dict) -> "InboundEvent":
        actor = data.get("actor")
        return cls(semantic_key=data["semantic_key"], kind=data["kind"],
                   origin=Origin.from_json(data["origin"]),
                   actor=ExternalActor(actor["external_id"], actor.get("display") or "",
                                       bool(actor.get("is_bot")), dict(actor.get("evidence") or {}))
                   if actor else None,
                   text=data.get("text") or "", trigger=bool(data.get("trigger", True)),
                   message_id=data.get("message_id") or "", files=list(data.get("files") or []),
                   data=dict(data.get("data") or {}), ignore_reason=data.get("ignore_reason"))


@dataclass(frozen=True)
class Outbound:
    """One reply part, already rendered for the provider."""

    kind: str           # ack, progress, result, notice, error
    text: str
    part: int = 1
    parts: int = 1
    marker: str = ""    # an opaque recovery marker the adapter embeds where it can
    blocks: Any = None


@dataclass(frozen=True)
class Receipt:
    """What the provider said about a post: its own message id and anything
    needed to find or update it later."""

    message_id: str
    data: dict = field(default_factory=dict)
