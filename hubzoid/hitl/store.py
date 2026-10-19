"""Requests and grants in the operational store.

A **request** is one thing a person must answer before work goes on: an
approval of one exact tool call or document, or a question. It is created by
the agent's run (chat, a workflow, hosted MCP), shown wherever that person
looks, and settled once, by compare-and-set on its ``revision``.

A **grant** is what an approval leaves behind for the agent:

* ``call``   one use, for the exact call that was approved (tool + argument digest)
* ``chat``   "Approve for this chat": the tool, in this conversation
* ``always`` "Always allow for me": the tool, for this person, in this hub

Grants never outlive the hub they belong to and are never exported.
"""
from __future__ import annotations

import json
import time
import uuid

import sqlalchemy as sa

metadata = sa.MetaData()

requests = sa.Table(
    "hz_hitl_requests", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("kind", sa.String(16), nullable=False),            # approval | question
    sa.Column("status", sa.String(16), nullable=False),          # pending | approved | rejected | answered | cancelled
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("origin", sa.String(16), nullable=False),          # chat | workflow | mcp | api
    sa.Column("requester_id", sa.String(255), nullable=False),   # account id (or email for older accounts)
    sa.Column("requester_email", sa.String(320)),
    sa.Column("approvers_json", sa.Text, nullable=False),        # ["requester"] or account ids
    sa.Column("title", sa.String(500), nullable=False),
    sa.Column("tool", sa.String(255)),
    sa.Column("arguments_json", sa.Text),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("question", sa.Text),
    sa.Column("options_json", sa.Text),
    sa.Column("subject_json", sa.Text),                          # a document approval: artifact, version, digest
    sa.Column("allow_chat", sa.Boolean, nullable=False, default=True),
    sa.Column("allow_always", sa.Boolean, nullable=False, default=False),
    sa.Column("conversation_id", sa.String(64), index=True),
    sa.Column("branch_id", sa.String(64)),
    sa.Column("message_id", sa.String(64)),
    sa.Column("run_id", sa.String(64)),
    sa.Column("workflow_id", sa.String(255)),
    sa.Column("decision", sa.String(24)),                        # approve | approve_chat | approve_always | reject | answer | request_changes
    sa.Column("answer", sa.Text),
    sa.Column("note", sa.Text),
    sa.Column("decided_by", sa.String(255)),
    sa.Column("decided_by_email", sa.String(320)),
    sa.Column("decided_via", sa.String(24)),                     # web | cli | slack | whatsapp
    sa.Column("continued", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("decided_at", sa.Float),
)

grants = sa.Table(
    "hz_hitl_grants", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("scope", sa.String(8), nullable=False),            # call | chat | always
    sa.Column("account_id", sa.String(255), nullable=False),
    sa.Column("tool", sa.String(255), nullable=False),
    sa.Column("digest", sa.String(80)),                          # scope call: the exact call
    sa.Column("conversation_id", sa.String(64)),                 # scope chat
    sa.Column("request_id", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("used_at", sa.Float),
)

events = sa.Table(
    "hz_hitl_events", metadata,
    sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("hub_id", sa.String(255), nullable=False, index=True),
    sa.Column("request_id", sa.String(64), nullable=False),
    sa.Column("type", sa.String(32), nullable=False),            # request.created | request.settled | request.cancelled
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

TERMINAL = ("approved", "rejected", "answered", "cancelled")


def now() -> float:
    return time.time()


def new_id() -> str:
    return str(uuid.uuid4())


def row_dict(row) -> dict:
    out = dict(row._mapping)
    for key in ("approvers_json", "arguments_json", "options_json", "subject_json"):
        raw = out.pop(key, None)
        out[key[:-5]] = json.loads(raw) if raw else None
    for key in ("created_at", "decided_at"):
        if out.get(key) is not None:
            out[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(out[key]))
    return out


def emit(conn, hub_id: str, request_id: str, kind: str, revision: int, payload: dict) -> None:
    conn.execute(events.insert().values(hub_id=hub_id, request_id=request_id, type=kind, revision=revision,
                                        payload_json=json.dumps(payload, default=str), created_at=now()))
