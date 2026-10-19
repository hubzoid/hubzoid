"""The one request service: when to ask, what to ask, and settling an answer.

Every surface calls these functions: the tool gate (chat, hosted MCP, a
channel), workflow blocks (``hub.approve``, ``hub.ask``), the HTTP routes and
the CLI. Nothing else writes a request or a grant.

How an approval flows (the same on every runtime):

1. The agent calls a tool whose rule asks. :func:`check` creates a pending
   request and the tool returns "waiting for approval" instead of running. The
   agent tells the person and ends its turn. No process waits for a person.
2. The person decides (:func:`decide`): approve, approve for this chat,
   always allow, or reject with a note. Only a person can: the routes refuse
   models, API keys and anyone who is not the requester or a named approver.
3. An approval leaves a **grant** for exactly that call (or the tool for the
   chat, or always for that person). The chat continues the same conversation:
   the agent is told the outcome and calls the tool again with the same
   arguments, which the grant now admits once. A rejection tells the agent not to.

So the tool itself only ever runs from the agent's own call, once, after the
approval: there is a single place that executes it.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import sqlalchemy as sa

from ..control import canonical
from . import config as cfglib
from . import store as st

log = logging.getLogger("hubzoid.hitl")

MAX_ARGS_BYTES = 64 * 1024
MAX_NOTE = 4000
MAX_ANSWER = 8000
DECISIONS = ("approve", "approve_chat", "approve_always", "reject", "answer", "request_changes")

#: origin -> callable(hub_dir, request) run after a request is settled
_continuers: dict[str, Callable[[Path, dict], Any]] = {}


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Caller:
    """Who is asking: the person the run acts for, and where they are."""

    account_id: str
    email: str = ""
    present: bool = True                 # a person can answer now (chat, a channel), not a schedule
    origin: str = "chat"                 # chat | mcp | slack | whatsapp | workflow | api
    conversation_id: str | None = None
    branch_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Verdict:
    """What the gate decided for one call."""

    action: str                          # allow | ask | refuse
    request: dict | None = None
    message: str = ""


def register_continuer(origin: str, fn: Callable[[Path, dict], Any]) -> None:
    _continuers[origin] = fn


def engine(hub_dir: Path):
    from .. import db

    return db.operational_engine(Path(hub_dir))


def call_digest(tool: str, arguments: Any) -> str:
    return canonical.digest({"tool": tool, "arguments": arguments})


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _same_person(account_id: str, email: str, request: dict) -> bool:
    return bool((account_id and account_id == request.get("requester_id"))
                or (email and _norm(email) == _norm(request.get("requester_email"))))


def eligible(request: dict, account_id: str, email: str) -> bool:
    """May this person decide the request: the requester by default, or a
    named approver. Being an administrator is not enough."""
    approvers = request.get("approvers") or ["requester"]
    for who in approvers:
        if who == "requester" and _same_person(account_id, email, request):
            return True
        if who != "requester" and (who == account_id or _norm(who) == _norm(email)):
            return True
    return False


# -- the gate -----------------------------------------------------------------------
def _grant_allows(conn, hub_id: str, caller: Caller, tool: str, digest: str, rule) -> bool:
    g = st.grants.c
    base = sa.and_(g.hub_id == hub_id, g.tool == tool,
                   sa.or_(g.account_id == caller.account_id, g.account_id == _norm(caller.email)))
    # One use of the exact call that was approved.
    row = conn.execute(sa.select(st.grants).where(base, g.scope == "call", g.digest == digest,
                                                  g.used_at.is_(None)).limit(1)).first()
    if row is not None:
        done = conn.execute(st.grants.update().where(g.id == row.id, g.used_at.is_(None))
                            .values(used_at=st.now())).rowcount
        return done == 1
    if rule.ask == "once" and caller.conversation_id:
        if conn.execute(sa.select(g.id).where(base, g.scope == "chat",
                                              g.conversation_id == caller.conversation_id).limit(1)).first():
            return True
    if rule.allow_always and conn.execute(sa.select(g.id).where(base, g.scope == "always").limit(1)).first():
        return True
    return False


def _title(tool: str, arguments: Any) -> str:
    words = tool.split("__")[-1].replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else tool


def check(hub_dir: Path, hub_id: str, caller: Caller, *, tool: str, arguments: Any,
          connector: str | None = None, read_only: bool | None = None,
          auto_check: Callable[[str, Any], bool] | None = None) -> Verdict:
    """Decide one tool call before it runs."""
    cfg = cfglib.load(Path(hub_dir))
    rule = cfg.rule_for(tool, connector)
    ask = rule.ask
    if ask == "writes":
        ask = "never" if read_only is True else "once"
        rule = cfglib.ToolRule(ask=ask, allow_always=rule.allow_always)
    if ask == "never":
        return Verdict("allow")
    try:
        encoded = json.dumps(arguments, default=str)
    except (TypeError, ValueError):
        encoded = json.dumps(str(arguments))
    if len(encoded.encode()) > MAX_ARGS_BYTES:
        return Verdict("refuse", message="This call is too large to show for approval, so it was not run.")
    digest = call_digest(tool, arguments)
    eng = engine(hub_dir)
    with eng.begin() as conn:
        if _grant_allows(conn, hub_id, caller, tool, digest, rule):
            return Verdict("allow")
    if ask == "auto" and auto_check is not None:
        try:
            if auto_check(tool, arguments):
                return Verdict("allow")
        except Exception:  # noqa: BLE001 - an unavailable check asks a person
            log.warning("hitl: auto check failed for %s; asking a person", tool, exc_info=True)
    if not caller.present:
        return Verdict("refuse", message=(
            f"{_title(tool, arguments)} needs a person's approval, and nobody can approve in an unattended "
            "run. It was not run. Say so in the result."))
    req = create(hub_dir, hub_id, caller, kind="approval", title=_title(tool, arguments), tool=tool,
                 arguments=arguments, digest=digest, approvers=cfg.approvers,
                 allow_chat=(ask == "once"), allow_always=rule.allow_always)
    return Verdict("ask", request=req)



def pending_for_workflow(hub_dir: Path, hub_id: str, workflow_id: str, kind: str, title: str,
                         digest: str | None) -> dict | None:
    """The pending request a workflow run already made with this kind, title and subject digest."""
    r = st.requests.c
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(st.requests).where(
            r.hub_id == hub_id, r.workflow_id == workflow_id, r.kind == kind, r.title == title,
            r.status == "pending")).fetchall()
    for row in rows:
        req = st.row_dict(row)
        if (req.get("subject") or {}).get("digest") == digest:
            return req
    return None


def count_for_workflow(hub_dir: Path, hub_id: str, workflow_id: str, title: str) -> int:
    """How many requests a workflow run already made with this title (plan versions)."""
    with engine(hub_dir).connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(st.requests).where(
            st.requests.c.hub_id == hub_id, st.requests.c.workflow_id == workflow_id,
            st.requests.c.title == title)).scalar() or 0)

# -- requests ----------------------------------------------------------------------
def create(hub_dir: Path, hub_id: str, caller: Caller, *, kind: str, title: str, tool: str | None = None,
           arguments: Any = None, digest: str | None = None, question: str | None = None,
           options: list | None = None, subject: dict | None = None, approvers=("requester",),
           allow_chat: bool = False, allow_always: bool = False, workflow_id: str | None = None) -> dict:
    """A new pending request. An identical pending one in the same chat turn
    (same tool and arguments, or same question) is returned instead."""
    if kind not in ("approval", "question"):
        raise RequestError(422, "validation_failed", "kind is approval or question.")
    if digest is None:
        digest = canonical.digest({"kind": kind, "question": question, "options": options,
                                   "subject": subject, "tool": tool, "arguments": arguments})
    r = st.requests.c
    with engine(hub_dir).begin() as conn:
        if caller.conversation_id or workflow_id:
            same = conn.execute(sa.select(st.requests).where(
                r.hub_id == hub_id, r.status == "pending", r.digest == digest,
                (r.conversation_id == caller.conversation_id) if caller.conversation_id
                else (r.workflow_id == workflow_id))).first()
            if same is not None:
                return st.row_dict(same)
        rid = st.new_id()
        conn.execute(st.requests.insert().values(
            id=rid, hub_id=hub_id, kind=kind, status="pending", revision=1, origin=caller.origin,
            requester_id=caller.account_id or _norm(caller.email), requester_email=_norm(caller.email) or None,
            approvers_json=json.dumps(list(approvers)), title=title[:500], tool=tool,
            arguments_json=json.dumps(arguments, default=str) if arguments is not None else None,
            digest=digest, question=question, options_json=json.dumps(options) if options else None,
            subject_json=json.dumps(subject) if subject else None, allow_chat=allow_chat,
            allow_always=allow_always, conversation_id=caller.conversation_id, branch_id=caller.branch_id,
            message_id=caller.message_id, run_id=caller.run_id, workflow_id=workflow_id, continued=False,
            created_at=st.now()))
        st.emit(conn, hub_id, rid, "request.created", 1, {"kind": kind, "title": title})
        row = conn.execute(sa.select(st.requests).where(r.id == rid)).first()
    return st.row_dict(row)


def get(hub_dir: Path, hub_id: str, request_id: str) -> dict | None:
    r = st.requests.c
    with engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(st.requests).where(r.id == request_id, r.hub_id == hub_id)).first()
    return st.row_dict(row) if row is not None else None


def visible_to(request: dict, account_id: str, email: str) -> bool:
    return _same_person(account_id, email, request) or eligible(request, account_id, email)


def get_for(hub_dir: Path, hub_id: str, request_id: str, account_id: str, email: str) -> dict:
    req = get(hub_dir, hub_id, request_id)
    if req is None or not visible_to(req, account_id, email):
        raise RequestError(404, "not_found", "No such request, or it is not yours to see.")
    return with_actions(req, account_id, email)


def with_actions(req: dict, account_id: str, email: str) -> dict:
    out = dict(req)
    can = req["status"] == "pending" and eligible(req, account_id, email)
    if not can:
        out["allowed_actions"] = []
    elif req["kind"] == "question":
        out["allowed_actions"] = ["answer"]
    elif req.get("subject"):
        out["allowed_actions"] = ["approve", "request_changes", "reject"]
    else:
        out["allowed_actions"] = (["approve"] + (["approve_chat"] if req.get("allow_chat") else [])
                                  + (["approve_always"] if req.get("allow_always") else []) + ["reject"])
    out["can_cancel"] = req["status"] == "pending" and _same_person(account_id, email, req)
    return out


def list_for(hub_dir: Path, hub_id: str, account_id: str, email: str, *, status: str | None = "pending",
             conversation_id: str | None = None, limit: int = 50) -> list[dict]:
    """Requests this person asked or may decide, newest first."""
    r = st.requests.c
    q = sa.select(st.requests).where(r.hub_id == hub_id)
    if status:
        q = q.where(r.status == status)
    if conversation_id:
        q = q.where(r.conversation_id == conversation_id)
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(q.order_by(r.created_at.desc()).limit(500)).fetchall()
    out = []
    for row in rows:
        req = st.row_dict(row)
        if visible_to(req, account_id, email):
            out.append(with_actions(req, account_id, email))
        if len(out) >= limit:
            break
    return out


def decide(hub_dir: Path, hub_id: str, request_id: str, *, account_id: str, email: str, decision: str,
           expected_revision: int | None = None, digest: str | None = None, answer: str | None = None,
           note: str | None = None, via: str = "web") -> dict:
    """Settle a pending request once. Raises RequestError for a stale card, a
    second decision or someone who may not decide."""
    if decision not in DECISIONS:
        raise RequestError(422, "validation_failed", f"decision is one of: {', '.join(DECISIONS)}.")
    note = (note or "").strip() or None
    if note and len(note) > MAX_NOTE:
        raise RequestError(422, "validation_failed", f"Keep the note under {MAX_NOTE} characters.")
    r = st.requests.c
    with engine(hub_dir).begin() as conn:
        row = conn.execute(sa.select(st.requests).where(r.id == request_id, r.hub_id == hub_id)).first()
        if row is None:
            raise RequestError(404, "not_found", "No such request.")
        req = st.row_dict(row)
        if not visible_to(req, account_id, email):
            raise RequestError(404, "not_found", "No such request.")
        if req["status"] != "pending":
            raise RequestError(409, "already_decided", f"This request was already {req['status']}.",
                               current_revision=req["revision"])
        if not eligible(req, account_id, email):
            raise RequestError(403, "permission_denied",
                               "Only the person who asked, or an approver named for this hub, can decide this.")
        if expected_revision is not None and int(expected_revision) != req["revision"]:
            raise RequestError(409, "revision_conflict", "This request changed. Reload it and decide again.",
                               current_revision=req["revision"])
        if digest is not None and digest != req["digest"]:
            raise RequestError(409, "digest_mismatch", "What you saw is not what is waiting now. Reload it.")
        allowed = with_actions(req, account_id, email)["allowed_actions"]
        if decision not in allowed:
            raise RequestError(422, "validation_failed",
                               f"This request takes: {', '.join(allowed) or 'nothing now'}.")
        if req["kind"] == "question":
            answer = (answer or "").strip()
            if not answer:
                raise RequestError(422, "validation_failed", "Type an answer or pick an option.")
            if len(answer) > MAX_ANSWER:
                raise RequestError(422, "validation_failed", f"Keep the answer under {MAX_ANSWER} characters.")
            # A picked option is its label, without the agent's "(Recommended)" marker.
            if any(answer == (o.get("label") or "") for o in (req.get("options") or [])):
                answer = re.sub(r"\s*\(recommended\)\s*$", "", answer, flags=re.I) or answer
        if decision == "request_changes" and not note:
            raise RequestError(422, "validation_failed", "Say what should change.")
        status = {"reject": "rejected", "answer": "answered", "request_changes": "rejected"}.get(decision, "approved")
        done = conn.execute(st.requests.update().where(
            r.id == request_id, r.status == "pending", r.revision == req["revision"]).values(
            status=status, revision=req["revision"] + 1, decision=decision, answer=answer if answer else None,
            note=note, decided_by=account_id or _norm(email), decided_by_email=_norm(email) or None,
            decided_via=via, decided_at=st.now())).rowcount
        if done != 1:
            raise RequestError(409, "already_decided", "Someone decided this a moment ago. Reload it.")
        if status == "approved" and req.get("tool"):
            scope = {"approve": "call", "approve_chat": "chat", "approve_always": "always"}[decision]
            conn.execute(st.grants.insert().values(
                id=st.new_id(), hub_id=hub_id, scope=scope, account_id=req["requester_id"], tool=req["tool"],
                digest=req["digest"], conversation_id=req.get("conversation_id"), request_id=request_id,
                created_at=st.now()))
            if scope != "call":  # the call that waited still runs once, then the wider grant applies
                conn.execute(st.grants.insert().values(
                    id=st.new_id(), hub_id=hub_id, scope="call", account_id=req["requester_id"],
                    tool=req["tool"], digest=req["digest"], conversation_id=req.get("conversation_id"),
                    request_id=request_id, created_at=st.now()))
        st.emit(conn, hub_id, request_id, "request.settled", req["revision"] + 1,
                {"status": status, "decision": decision})
        settled = st.row_dict(conn.execute(sa.select(st.requests).where(r.id == request_id)).first())
    _continue(hub_dir, settled)
    return with_actions(settled, account_id, email)


def cancel(hub_dir: Path, hub_id: str, request_id: str, *, account_id: str | None = None,
           email: str | None = None, reason: str = "cancelled", system: bool = False) -> dict | None:
    """Cancel a pending request: by its requester, or by the system (the
    person moved on in the chat, the run was stopped). Returns the request,
    or None when it was not pending."""
    r = st.requests.c
    with engine(hub_dir).begin() as conn:
        row = conn.execute(sa.select(st.requests).where(r.id == request_id, r.hub_id == hub_id)).first()
        if row is None:
            raise RequestError(404, "not_found", "No such request.")
        req = st.row_dict(row)
        if not system and not _same_person(account_id or "", email or "", req):
            raise RequestError(403, "permission_denied", "Only the person who asked can cancel this.")
        if req["status"] != "pending":
            return None
        conn.execute(st.requests.update().where(r.id == request_id, r.status == "pending").values(
            status="cancelled", revision=req["revision"] + 1, note=reason[:MAX_NOTE], decided_at=st.now(),
            decided_by=account_id or "system"))
        st.emit(conn, hub_id, request_id, "request.cancelled", req["revision"] + 1, {"reason": reason})
        return st.row_dict(conn.execute(sa.select(st.requests).where(r.id == request_id)).first())


def cancel_waiting_in_branch(hub_dir: Path, hub_id: str, branch_id: str, *, reason: str) -> int:
    """The person sent something new on this branch instead of answering:
    their waiting requests there are cancelled, so the new message steers."""
    r = st.requests.c
    with engine(hub_dir).connect() as conn:
        ids = [x.id for x in conn.execute(sa.select(r.id).where(
            r.hub_id == hub_id, r.branch_id == branch_id, r.status == "pending")).fetchall()]
    for rid in ids:
        cancel(hub_dir, hub_id, rid, system=True, reason=reason)
    return len(ids)


def mark_continued(hub_dir: Path, request_id: str) -> bool:
    """Claim the one continuation of a settled request (False: already claimed)."""
    r = st.requests.c
    with engine(hub_dir).begin() as conn:
        return conn.execute(st.requests.update().where(r.id == request_id, r.continued.is_(False))
                            .values(continued=True)).rowcount == 1


def _continue(hub_dir: Path, request: dict) -> None:
    fn = _continuers.get(request.get("origin") or "")
    if fn is None and request.get("workflow_id"):
        fn = _continuers.get("workflow")
    if fn is None:
        return
    try:
        fn(Path(hub_dir), request)
    except Exception:  # noqa: BLE001 - the decision stands; the continuation is retried on next view
        log.exception("hitl: could not continue after request %s", request.get("id"))


def outcome_text(request: dict) -> str:
    """What the agent is told when it continues, as data, never instructions
    from the person beyond what they chose."""
    who = request.get("decided_by_email") or "The person"
    status = request["status"]
    if request["kind"] == "question":
        return (f"[Answer to your question \"{request.get('question') or request['title']}\"]\n"
                f"{request.get('answer') or ''}\nContinue the task with this answer.")
    if request.get("subject"):
        if status == "approved":
            return f"[Plan approved] {who} approved \"{request['title']}\" exactly as shown. Go ahead with it."
        note = f" Their note: {request['note']}" if request.get("note") else ""
        return f"[Plan not approved] {who} asked for changes to \"{request['title']}\".{note} Revise it."
    args = json.dumps(request.get("arguments"), ensure_ascii=False, default=str)
    if status == "approved":
        extra = {"approve_chat": " It may run again in this chat without asking.",
                 "approve_always": " It may run again without asking."}.get(request.get("decision") or "", "")
        return (f"[Approval] {who} approved {request['tool']} with exactly these arguments: {args}.{extra}\n"
                "Call it again now with exactly the same arguments, then continue the task.")
    note = f" Their note: {request['note']}" if request.get("note") else ""
    return (f"[Approval] {who} rejected {request['tool']} with arguments {args}.{note}\n"
            "Do not run it. Continue without it, or ask what they would like instead.")


def visible_outcome(request: dict) -> str:
    """The short line shown as the person's reply in the chat."""
    if request["kind"] == "question":
        return request.get("answer") or ""
    if request.get("subject"):
        return "Approved" if request["status"] == "approved" else f"Changes requested: {request.get('note') or ''}"
    if request["status"] == "approved":
        return {"approve_chat": "Approved for this chat", "approve_always": "Always allowed"}.get(
            request.get("decision") or "", "Approved")
    return "Rejected" + (f": {request['note']}" if request.get("note") else "")


def events_after(hub_dir: Path, hub_id: str, after: int, limit: int = 200, *,
                 account_id: str | None = None, email: str | None = None) -> list[dict]:
    """Events after ``after``, only for requests the caller may see."""
    e = st.events.c
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(st.events).where(e.hub_id == hub_id, e.sequence > after)
                            .order_by(e.sequence).limit(limit)).fetchall()
    seen: dict[str, bool] = {}
    out = []
    for x in rows:
        if x.request_id not in seen:
            req = get(hub_dir, hub_id, x.request_id)
            seen[x.request_id] = bool(req) and visible_to(req, account_id or "", email or "")
        if seen[x.request_id]:
            out.append({"sequence": x.sequence, "request_id": x.request_id, "type": x.type,
                        "revision": x.revision, "payload": json.loads(x.payload_json)})
    return out
