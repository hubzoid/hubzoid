"""Request and response shapes of the public API, for the reference only.

The handlers keep reading their bodies as they always have (``read_json`` with
its size limits, aliases and error payloads), so these models describe the
API without changing what it accepts. Every model allows fields it does not
name. ``openapi_doc`` attaches them to the published routes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Doc(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---- OpenAI-compatible chat ------------------------------------------------------------

class ChatMessage(_Doc):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] = Field(
        description="Text, or OpenAI content parts. A file you uploaded is "
                    '{"type": "file", "file": {"file_id": "..."}}.')


class ChatCompletionRequest(_Doc):
    model: str | None = Field(None, description="The agent's model id from GET /v1/models.")
    messages: list[ChatMessage]
    stream: bool = False


class ChatCompletionChoice(_Doc):
    index: int
    message: dict[str, Any]
    finish_reason: str | None = None


class ChatCompletion(_Doc):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]


# ---- conversations and runs ------------------------------------------------------------

class ConversationCreate(_Doc):
    agent: str | None = Field(None, description="The agent's model id (or agent_id).")
    id: str | None = Field(None, description="Your own id for the conversation (c_...), optional.")
    title: str | None = None
    temporary: bool = False


class ContentPart(_Doc):
    type: Literal["text", "file"]
    text: str | None = None
    file_id: str | None = Field(None, description="For type file: an id from POST /api/chat/files.")


class Operation(_Doc):
    kind: Literal["send", "respond", "revise", "regenerate", "continue", "delete", "fork",
                  "synthesize", "compare"]
    branch_id: str
    parent_id: str | None = None
    content: list[ContentPart] | None = None
    expected_revision: int | None = None


class OperationAccepted(_Doc):
    run_id: str | None = Field(None, description="Follow it with GET /api/chat/runs/{run_id} or the "
                                                  "conversation's events.")


class RunStatus(_Doc):
    status: Literal["queued", "running", "waiting", "complete", "stopped", "interrupted", "failed"] | str


class ConversationEvent(_Doc):
    """One Server-Sent Event of GET /api/conversations/{cid}/events. The SSE
    ``id`` is the cursor: reconnect with ``Last-Event-ID`` (or ``?after=``)
    and the stream replays what you missed. An old cursor answers 410
    ``replay_expired``: reload the conversation. Replay reads the database,
    so it works on any replica; live wake-ups on another replica arrive on
    the 15 second keep-alive."""
    type: str = Field(description="For example message.updated, conversation.titled, access.revoked.")


# ---- files -----------------------------------------------------------------------------

class FileUpload(_Doc):
    file: bytes = Field(description="The file, as the multipart field 'file'.")


class StoredFile(_Doc):
    file_id: str
    name: str
    mime: str
    size: int


# ---- approvals and questions -----------------------------------------------------------

class Request_(_Doc):
    id: str
    kind: Literal["approval", "question"]
    status: str
    title: str
    revision: int
    digest: str
    allowed_actions: list[str] = Field(default_factory=list)


class RequestList(_Doc):
    items: list[Request_]
    pending_count: int


class Decision(_Doc):
    decision: Literal["approve", "approve_chat", "approve_always", "reject", "request_changes", "answer"]
    expected_revision: int | None = None
    digest: str | None = Field(None, description="The digest you saw: the decision fails if it changed.")
    answer: str | None = Field(None, description="For a question.")
    note: str | None = None


class CancelRequest(_Doc):
    note: str | None = None


class RequestEvent(_Doc):
    sequence: int
    request_id: str
    type: Literal["request.created", "request.settled", "request.cancelled"] | str
    revision: int
    payload: dict[str, Any]


class RequestEvents(_Doc):
    """GET /api/requests/events?after=N is polling, not a stream: pass the
    last ``sequence`` you saw as ``after``."""
    items: list[RequestEvent]


# ---- hub changes and memory ------------------------------------------------------------

class HubEdit(_Doc):
    path: str = Field(description="A path inside the hub, such as knowledge/pricing.md.")
    content: str | None = Field(None, description="The whole new text.")
    append: str | None = Field(None, description="Text to add at the end.")
    old_text: str | None = Field(None, description="Exact text to replace (with new_text).")
    new_text: str | None = None


class HubChangeProposal(_Doc):
    title: str
    edits: list[HubEdit]
    reason: str | None = None


class HubChangeDecision(_Doc):
    decision: Literal["approve", "reject"]
    expected_revision: int | None = None
    note: str | None = None


class MemoryPut(_Doc):
    text: str
    expected_revision: int | None = None


# ---- notification webhooks -------------------------------------------------------------

class NotificationTarget(_Doc):
    type: Literal["webhook"]
    recipient_ref: str = Field(description="An https:// address on the public internet.")
    connection_ref: str = Field(description="env:NAME of the signing secret.")
    events: list[Literal["run.completed", "run.failed", "run.interrupted", "request.created"]]
    enabled: bool = False
    expected_revision: int | Literal["absent"] | None = None


class WebhookPayload(_Doc):
    """What a webhook receives. Headers: ``x-hubzoid-timestamp`` and
    ``x-hubzoid-signature`` = ``v1=`` + HMAC-SHA256(secret, timestamp + "." +
    raw body). Check both and refuse a timestamp older than five minutes."""
    type: Literal["run.completed", "run.failed", "run.interrupted", "request.created"]
    event_id: str
    occurred_at: float
    conversation_id: str | None = None
    request_id: str | None = Field(None, description="request.created only.")
    kind: str | None = Field(None, description="request.created only: approval or question.")
    url: str | None = Field(None, description="request.created only: where to decide it.")


# ---- embedded chat (AG-UI) -------------------------------------------------------------

class AguiRunInput(_Doc):
    """AG-UI RunAgentInput (https://docs.ag-ui.com)."""
    threadId: str
    runId: str
    messages: list[dict[str, Any]]
    state: Any = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)


# ---- run for me ------------------------------------------------------------------------

class MyRunStarted(_Doc):
    run_id: str
    status: str
    created: bool = Field(description="False when an Idempotency-Key or an active run with the same inputs "
                                      "returned an existing run (200).")
    hub: str
    result: str = Field(description="GET this to read the run's status and report links.")


#: (method, path) -> what the reference shows. "request" is a model (JSON) or
#: ("multipart", model); "response" is a model, ("sse", model) or None.
SHAPES: dict[tuple[str, str], dict] = {
    ("POST", "/v1/chat/completions"): {"request": ChatCompletionRequest, "response": ChatCompletion},
    ("POST", "/api/conversations"): {"request": ConversationCreate},
    ("POST", "/api/conversations/{cid}/operations"): {"request": Operation, "response": OperationAccepted,
                                                      "status": "202"},
    ("GET", "/api/chat/runs/{run_id}"): {"response": RunStatus},
    ("GET", "/api/conversations/{cid}/events"): {"response": ("sse", ConversationEvent)},
    ("POST", "/api/chat/files"): {"request": ("multipart", FileUpload), "response": StoredFile, "status": "201"},
    ("GET", "/api/requests"): {"response": RequestList},
    ("GET", "/api/requests/events"): {"response": RequestEvents},
    ("POST", "/api/requests/{rid}/decisions"): {"request": Decision},
    ("POST", "/api/requests/{rid}/cancel"): {"request": CancelRequest},
    ("POST", "/api/hub-changes"): {"request": HubChangeProposal},
    ("POST", "/api/hub-changes/{cid}/decision"): {"request": HubChangeDecision},
    ("PUT", "/api/memory"): {"request": MemoryPut},
    ("PUT", "/api/chat/notification-targets/{target_id}"): {"request": NotificationTarget},
    ("POST", "/api/agui"): {"request": AguiRunInput, "response": ("sse", None)},
    ("GET", "/api/agui/threads/{thread_id}/run"): {"response": ("sse", None)},
    ("POST", "/portal/api/my/runs"): {"response": MyRunStarted, "status": "202"},
}

#: Shapes shown in the reference that no route returns directly.
EXTRA = (WebhookPayload,)
