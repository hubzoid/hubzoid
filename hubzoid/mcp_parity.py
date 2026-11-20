# Hubzoid MCP server. Apache-2.0 licensed like the rest of the repository.
"""What the hosted MCP surface adds so a connected assistant can do what chat does.

See ``proposals/2026-11-20-mcp-parity.md``. ``mcp_server`` builds the tool
list and calls into here:

  * **A file space per person.** Over MCP there is no chat, so each person gets
    their own space, ``chats/mcp-<hash>/`` (``space_id``), bound for each call
    exactly as chat binds a chat id. ``write_artifact``, ``make_excel`` and the
    other file tools then work unchanged and return the same signed download
    link. ``upload_file`` puts a file the person gives their assistant into the
    same space, where ``read_upload`` reads it (MCP has no upload of its own).
  * **Questions and approvals in place.** With a client that supports MCP
    elicitation (and a session, see ``build_mcp_app``), ``ask_question`` asks
    the person and returns the answer, and an approval the caller may give
    themselves is asked the same way, recorded in the approvals store and the
    call then runs once. Otherwise the assistant is told to ask the person
    itself, or where to approve and to call again.
  * **Sub-agents on their own model** (``delegate_tools``), behind the
    ``mcp_delegates`` grant: the hub's own runtime runs once, as the caller,
    and hands the task to that sub-agent, so the hub pays for those model
    calls only for people an administrator chose.

Runtime neutral: the delegate tool calls ``runtime.build``, which picks the
hub's backend; nothing here constructs an SDK agent.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
from pathlib import Path

from .capabilities import Capability, register

log = logging.getLogger("hubzoid.mcp")

SPACE_PREFIX = "mcp-"
#: Largest file ``upload_file`` takes, decoded. Its base64 must fit the MCP
#: SDK's 4 MiB request body limit with room for the JSON around it.
MAX_INLINE_UPLOAD = int(2.5 * 1024 * 1024)
#: How long the person has to answer an elicitation before the call falls back.
ELICIT_SECONDS = 600.0
#: One delegate run, start to answer.
DELEGATE_SECONDS = 600.0

def _delegates_probe(hub_dir: Path) -> str:
    from . import factory, runtime
    from . import settings as settingslib

    hub_model = runtime._resolve_model_id(Path(hub_dir), settingslib.load(Path(hub_dir)))
    if factory._load_skills_and_delegates(Path(hub_dir), hub_model)[1]:
        return ""
    return "No sub-agents with their own model in this agent"


MCP_DELEGATES = register(Capability(
    permission="mcp_delegates", label="Use sub-agents on this agent's model from an assistant",
    group="tools", surfaces=("mcp",), requires=("MCP_SERVER",), missing="MCP is off",
    description="Call the sub-agents that run on their own model from Claude Code or another assistant "
                "connected over MCP. This agent pays for those model calls.",
    probe=_delegates_probe,
))


# ---------------------------------------------------------------------------
# The person's file space
# ---------------------------------------------------------------------------
def space_id(ident) -> str | None:
    """The chat id an MCP caller's files live under: one per account (the
    email when the token names no account). None for nobody."""
    key = (getattr(ident, "account_id", None) or getattr(ident, "user", None) or "").strip().lower()
    if not key or getattr(ident, "is_anonymous", False):
        return None
    return SPACE_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:24]


UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string", "minLength": 1, "maxLength": 200,
                     "description": "Bare file name with its extension, such as \"invoice.pdf\"."},
        "content": {"type": "string",
                    "description": "The file's content: its text, or base64 for a binary file."},
        "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text",
                     "description": "\"text\" for a text file (UTF-8), \"base64\" for anything else "
                                    "(PDF, Excel, Word, images)."},
    },
    "required": ["filename", "content"],
    "additionalProperties": False,
}


def upload_tool(hub_dir: Path, max_bytes: int):
    """``upload_file``: save a file the person gave their assistant into their
    MCP file space, for ``read_upload`` and the hub's tools that read uploads."""
    from agents import FunctionTool

    from . import _request_ctx
    from . import memory as memlib
    from . import uploads
    from .tools.files import _format_size, _safe_filename

    limit = max(1, min(int(max_bytes or MAX_INLINE_UPLOAD), MAX_INLINE_UPLOAD))

    async def invoke(_ctx, input_str: str) -> str:
        try:
            args = json.loads(input_str or "{}") or {}
        except ValueError:
            return "[upload_file: the arguments are not valid JSON.]"
        chat = _request_ctx.get_chat_id()
        if not chat:
            return "[upload_file: sign in to upload a file.]"
        name = _safe_filename(str(args.get("filename") or ""))
        if not name or uploads.is_sidecar(name):
            return "[upload_file: give a plain file name such as \"report.pdf\".]"
        content = args.get("content")
        if not isinstance(content, str):
            return "[upload_file: content must be a string.]"
        if (args.get("encoding") or "text") == "base64":
            try:
                payload = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                return "[upload_file: content is not valid base64.]"
        else:
            payload = content.encode("utf-8")
        if len(payload) > limit:
            return (f"[upload_file: {name} is {_format_size(len(payload))}; the limit over MCP is "
                    f"{_format_size(limit)}. Upload larger files in the Hubzoid web app.]")
        mime = uploads.guess_mime(name)
        target = memlib.chat_upload_dir(hub_dir, chat)

        def save() -> None:
            uploads.write_with_meta(target, name, payload, mime=mime)

        await asyncio.to_thread(save)
        return (f"Saved {name} ({_format_size(len(payload))}) for this agent. Read it with "
                f"read_upload(\"{name}\"), or give the name to a tool that works on an uploaded file.")

    return FunctionTool(
        name="upload_file", params_json_schema=UPLOAD_SCHEMA, on_invoke_tool=invoke, strict_json_schema=False,
        description=("Give this agent a file the person shared with you, so its tools can read it "
                     f"(read_upload, read_upload_full). Up to {_format_size(limit)}. Send text files as "
                     "text and anything else as base64. The file stays in the person's own space."))


def read_own_file(hub_dir: Path, ident, name: str) -> tuple[bytes, str] | None:
    """A file the caller made (their artifacts), for the ``hubzoid-file``
    resource: (bytes, mime), or None when there is no such file of theirs."""
    from . import memory as memlib
    from . import uploads
    from .tools.files import _safe_filename

    chat = space_id(ident)
    safe = _safe_filename(name or "")
    if not chat or not safe or safe != name:
        return None
    base = memlib.chat_artifact_dir(hub_dir, chat).resolve()
    target = (base / safe).resolve()
    if base not in target.parents or not target.is_file():
        return None
    return target.read_bytes(), uploads.guess_mime(safe)


def resource_links(artifacts: list) -> list:
    """An MCP ``resource_link`` for each file a call made, so an assistant can
    read the file back (``resources/read``) as well as show its link."""
    from urllib.parse import quote

    from mcp.types import ResourceLink

    from . import uploads

    out = []
    for item in artifacts or ():
        name = str(item.get("name") or "")
        if name:
            out.append(ResourceLink(type="resource_link", uri=f"hubzoid-file://artifacts/{quote(name, safe='')}",
                                    name=name, mimeType=uploads.guess_mime(name),
                                    description=f"Download: {item.get('url')}" if item.get("url") else None))
    return out


# ---------------------------------------------------------------------------
# Elicitation: questions and approvals asked of the person
# ---------------------------------------------------------------------------
def _context():
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except Exception:  # noqa: BLE001 - outside an MCP request
        return None


def can_elicit(ctx=None) -> bool:
    """Whether this caller's client said it can show the person a form."""
    ctx = ctx if ctx is not None else _context()
    if ctx is None:
        return False
    try:
        from mcp import types

        return bool(ctx.session.check_client_capability(
            types.ClientCapabilities(elicitation=types.ElicitationCapability())))
    except Exception:  # noqa: BLE001 - no session (stateless) or no client params
        return False


async def _elicit(ctx, message: str, response_type):
    try:
        return await asyncio.wait_for(ctx.elicit(message, response_type), ELICIT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the client could not ask: fall back
        log.info("mcp: elicitation not completed (%s)", type(exc).__name__)
        return None


def ask_question_tool(original):
    """``ask_question`` for MCP: the same name and schema as chat's, answered
    through elicitation, or handed to the assistant to ask itself."""
    from . import tool_classes
    from .hitl import tools as hitl_tools

    async def invoke(_ctx, raw) -> str:
        try:
            question, options = hitl_tools._validate(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        ctx = _context()
        if can_elicit(ctx):
            labels = [o["label"] for o in options or ()]
            text = question + "".join(f"\n- {o['label']}: {o['description']}" for o in options or ()
                                      if o["description"])
            result = await _elicit(ctx, text, labels or str)
            if result is not None and result.action == "accept":
                return json.dumps({"status": "answered", "answer": result.data})
            if result is not None:
                return json.dumps({"status": "declined", "message":
                                   "The person chose not to answer. Continue with your best judgment and "
                                   "state the assumption you made."})
        return json.dumps({"status": "ask_in_conversation", "question": question, "options": options,
                           "message": "Ask the person this question yourself, in your conversation with "
                                      "them, and continue with their answer."})

    description = hitl_tools.DESCRIPTION.replace(
        "- After asking, end your turn. The answer arrives as the user's next message.",
        "- The answer comes back in this tool's result. If the status is \"ask_in_conversation\", ask the "
        "person yourself and continue with their answer.")
    return tool_classes.copy_tool(original, on_invoke_tool=invoke, description=description)


def _waiting(text) -> dict | None:
    """The approval gate's "waiting" answer, parsed, or None."""
    if not isinstance(text, str) or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("status") == "waiting" and data.get("request_id"):
        return data
    return None


#: The longest arguments shown in a form. Longer ones are approved in the inbox,
#: where the person sees every character.
ELICIT_ARGS_MAX = 4000


def _created_at(req: dict) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(str(req.get("created_at")), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def _shown_arguments(req: dict) -> str | None:
    """The request's arguments as the person will see them, whole, or None when
    a form cannot show all of it (too long, or a plan document to open)."""
    if req.get("subject"):
        return None
    if req.get("arguments") is None:
        return ""
    args = json.dumps(req.get("arguments"), default=str)
    return args if len(args) <= ELICIT_ARGS_MAX else None


async def settle_waiting(hub_dir: Path, ident, text: str, rerun, *, since: float):
    """When a call waits for a person: ask the caller in place if they may
    decide and their client can show a form. A tool's approval is recorded and
    the call runs once (``rerun``); a workflow run's question or approval is
    answered and the run carries on (``_settle_run_request``). Otherwise say
    where to decide. Any other result comes back unchanged.

    Only a request this very call created (``since``) that the caller may see
    is settled: a tool result that merely contains such JSON (a file, a web
    page) never reaches another request. ``rerun`` may return text or a
    ToolResult, which is passed back as it is."""
    waiting = _waiting(text)
    if waiting is None:
        return text
    from .hitl import gate, service

    hub_id = gate.hub_id_of(hub_dir)
    req = await asyncio.to_thread(service.get, hub_dir, hub_id, waiting["request_id"])
    account, email = getattr(ident, "account_id", None) or ident.user or "", ident.user or ""
    if (not req or req.get("status") != "pending" or _created_at(req) < since - 2
            or not service.visible_to(req, account, email)):
        return text
    mine = service.eligible(req, account, email)
    ctx = _context()
    if not req.get("tool"):
        return await _settle_run_request(hub_dir, hub_id, req, account, email, mine, ctx)
    args = _shown_arguments(req)
    if mine and args is not None and can_elicit(ctx):
        message = f"Approve \"{req.get('title') or req['tool']}\"?" + (f"\n\nWith: {args}" if args else "")
        result = await _elicit(ctx, message, ["Approve", "Reject"])
        if result is not None and result.action == "accept" and result.data in ("Approve", "Reject"):
            decision = "approve" if result.data == "Approve" else "reject"
            try:
                await asyncio.to_thread(service.decide, hub_dir, hub_id, req["id"], account_id=account,
                                        email=email, decision=decision, via="mcp")
            except service.RequestError as exc:
                return json.dumps({"status": "not_run", "message": f"Not run: {exc}"})
            if decision == "reject":
                return json.dumps({"status": "rejected", "message":
                                   f"Not run: the person rejected \"{req.get('title') or req['tool']}\"."})
            return await rerun()
    where = service.requests_url()
    who = "The person" if mine else "Someone who may approve it"
    return json.dumps({
        "status": "waiting", "request_id": req["id"], "kind": req.get("kind"),
        "message": (f"Waiting for approval: \"{req.get('title') or req['tool']}\" has not run. {who} can "
                    f"approve it in Hubzoid ({where}). Tell the person in one sentence. Once it is approved, "
                    f"call {req['tool']} again with exactly the same arguments; it then runs once.")})


#: How long to wait for a run's next question after one is answered in place.
NEXT_REQUEST_SECONDS = 20.0


async def _settle_run_request(hub_dir: Path, hub_id: str, req: dict, account: str, email: str, mine: bool,
                              ctx) -> str:
    """A workflow run started over MCP stopped to ask (``hub.ask``) or for an
    approval (``hub.approve``). When the caller may decide and their client can
    show a form, ask in place, record the decision, and follow the run to its
    next request the same way (a few seconds each). Otherwise say where to
    decide. The run's result is read with ``my_reports`` or ``workflow_runs``."""
    from .hitl import service

    follow = "The run carries on by itself. Check its result with my_reports or workflow_runs."
    done: list[str] = []
    while req is not None:
        title = req.get("title") or "this run"
        if not (service.eligible(req, account, email) and can_elicit(ctx)):
            break
        decision, answer = await _ask_run_request(ctx, req, title)
        if decision is None:
            break
        try:
            await asyncio.to_thread(service.decide, hub_dir, hub_id, req["id"], account_id=account,
                                    email=email, decision=decision, answer=answer, via="mcp")
        except service.RequestError as exc:
            done.append(f"Not recorded for \"{title}\": {exc}.")
            break
        done.append({"answer": f"Answered \"{title}\": {answer}.", "approve": f"Approved \"{title}\".",
                     "reject": f"Rejected \"{title}\"."}[decision])
        req = await _next_run_request(hub_dir, hub_id, req, account, email)
    if req is None:
        return json.dumps({"status": "decided", "message": " ".join(done + [follow])})
    who = "The person" if service.eligible(req, account, email) else "Someone who may decide it"
    verb = "answer" if req.get("kind") == "question" else "approve"
    return json.dumps({
        "status": "waiting", "request_id": req["id"], "kind": req.get("kind"),
        "message": " ".join(done + [
            f"The run is waiting: \"{req.get('title') or 'this run'}\". {who} can {verb} it in Hubzoid "
            f"({service.requests_url()}). Tell the person in one sentence. {follow}"])})


async def _ask_run_request(ctx, req: dict, title: str) -> tuple[str | None, str | None]:
    """(decision, answer) from the person through elicitation, or (None, None)."""
    if req.get("kind") == "question":
        labels = [o.get("label") for o in req.get("options") or () if o.get("label")]
        result = await _elicit(ctx, req.get("question") or title, labels or str)
        if result is None or result.action != "accept" or not str(result.data or "").strip():
            return None, None
        return "answer", str(result.data)
    args = _shown_arguments(req)
    if args is None:   # a plan to open, or more than a form shows: the inbox
        return None, None
    result = await _elicit(ctx, f"Approve \"{title}\"?" + (f"\n\nWith: {args}" if args else ""),
                           ["Approve", "Reject"])
    if result is None or result.action != "accept" or result.data not in ("Approve", "Reject"):
        return None, None
    return ("approve" if result.data == "Approve" else "reject"), None


async def _next_run_request(hub_dir: Path, hub_id: str, req: dict, account: str, email: str) -> dict | None:
    """The same run's next pending request, if it raises one soon."""
    from .hitl import service

    run = req.get("workflow_id")
    if not run:
        return None
    deadline = time.monotonic() + NEXT_REQUEST_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        pending = await asyncio.to_thread(service.list_for, hub_dir, hub_id, account, email)
        for nxt in pending:
            if nxt.get("workflow_id") == run and nxt["id"] != req["id"]:
                return nxt
    return None


# ---------------------------------------------------------------------------
# Sub-agents on their own model, behind a grant
# ---------------------------------------------------------------------------
DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1,
                            "description": "The whole task for the sub-agent, with the context it needs. "
                                           "It does not see your conversation."}},
    "required": ["task"],
    "additionalProperties": False,
}


def delegate_tools(hub_dir: Path, settings) -> dict:
    """``{handover_<name>: FunctionTool}`` for the hub's model delegates (an
    agent whose ``model:`` differs from the hub's on the same engine), each
    behind the ``mcp_delegates`` grant. Empty when the hub has none."""
    from agents import FunctionTool

    from . import factory, handover, runtime
    from .access.guard import guard_tool

    try:
        hub_model = runtime._resolve_model_id(Path(hub_dir), settings)
        _skills, delegates = factory._load_skills_and_delegates(Path(hub_dir), hub_model)
    except Exception:  # noqa: BLE001 - no delegates rather than no MCP
        log.warning("mcp: sub-agents could not be read; none offered", exc_info=True)
        return {}
    out: dict = {}
    for loaded in delegates:
        name = handover.tool_name(loaded.spec.name)
        agent = loaded.spec.name

        async def invoke(_ctx, input_str: str, _agent=agent, _name=name) -> str:
            try:
                task = str((json.loads(input_str or "{}") or {}).get("task") or "").strip()
            except ValueError:
                task = ""
            if not task:
                return f"[{_name}: give the task to do.]"
            return await run_delegate(Path(hub_dir), _agent, _name, task)

        description = " ".join(filter(None, [
            (loaded.spec.description or "").strip(),
            f"Runs the {agent} sub-agent on its own model ({loaded.spec.model}), paid by this agent. "
            "It does not see your conversation: put everything it needs in the task."]))
        ft = FunctionTool(name=name, description=description, params_json_schema=DELEGATE_SCHEMA,
                          on_invoke_tool=invoke, strict_json_schema=False)
        out[name] = guard_tool(ft, MCP_DELEGATES.permission, Path(hub_dir), surfaces=frozenset({"mcp"}))
    return out


async def run_delegate(hub_dir: Path, agent: str, tool: str, task: str) -> str:
    """One run of the hub's own runtime, as the caller (already bound), that
    hands ``task`` to the ``agent`` sub-agent. Usage is recorded under the
    caller with surface ``mcp``."""
    from . import _request_ctx, run_events, runtime
    from . import usage as usage_lib
    from .access import current_identity

    ident = current_identity()
    prompt = (f"Hand the task below to the `{agent}` sub-agent with its tool, and reply with its answer "
              f"as it gave it. Do not do the task yourself.\n\nTask:\n{task}")
    started = time.monotonic()
    started_at = time.time()
    raw: dict = {}
    status = "error"
    rt = runtime.build(hub_dir)
    try:
        await rt.aopen()
        try:
            from . import guardrails

            # The guardrails check what the person sent (the task), not the handover wording.
            token = guardrails.NEW_INPUT.set(task)
            try:
                async with asyncio.timeout(DELEGATE_SECONDS):
                    text = await run_events.answer_only(rt, prompt)
            finally:
                guardrails.NEW_INPUT.reset(token)
        finally:
            raw.update(_request_ctx.drain_usage())
        err = getattr(rt, "last_error", None)
        if err is not None:
            return f"[{tool}: the sub-agent did not finish ({type(err).__name__}). Try again shortly.]"
        status = "ok"
        return text + await asyncio.to_thread(_left_waiting, hub_dir, ident, started_at)
    except TimeoutError:
        return f"[{tool}: the sub-agent took longer than {int(DELEGATE_SECONDS)} seconds and was stopped.]"
    finally:
        await rt.aclose()
        try:
            usage_lib.record(hub_dir, hub=Path(hub_dir).name, surface="mcp", kind="delegate",
                             subject=ident.user, model=raw.get("model"),
                             input_tokens=raw.get("input_tokens"), output_tokens=raw.get("output_tokens"),
                             cost_usd=raw.get("cost_usd"), status=status,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             reservation=raw.get("budget_reservation"), trigger="mcp",
                             **usage_lib.usage_details(raw))
        except Exception:  # noqa: BLE001 - the answer is not lost for a usage row
            log.warning("mcp: delegate usage not recorded", exc_info=True)


def _left_waiting(hub_dir: Path, ident, since: float) -> str:
    """What the finished sub-agent run left waiting for this person. Its run is
    over, so a question it asked can no longer be answered: it is withdrawn.
    An approval stays in the inbox; once approved, the same call runs once
    when the sub-agent is asked again. Returns a note for the result, or ""."""
    from .hitl import gate, service

    hub_id = gate.hub_id_of(hub_dir)
    account, email = getattr(ident, "account_id", None) or ident.user or "", ident.user or ""
    try:
        pending = [r for r in service.list_for(hub_dir, hub_id, account, email)
                   if _created_at(r) >= since - 2 and not r.get("workflow_id")]
    except Exception:  # noqa: BLE001 - the answer stands without the note
        log.warning("mcp: requests from a sub-agent run could not be read", exc_info=True)
        return ""
    notes = []
    for req in pending:
        if req.get("kind") == "question":
            try:
                service.cancel(hub_dir, hub_id, req["id"], account_id=account, email=email,
                               reason="the sub-agent run ended")
            except service.RequestError:
                pass
            notes.append(f"It asked \"{req.get('title')}\", which cannot be answered after its run: "
                         "ask the person and call the sub-agent again with the answer in the task.")
        else:
            notes.append(f"\"{req.get('title')}\" is waiting for approval in Hubzoid "
                         f"({service.requests_url()}). Once approved, ask the sub-agent again.")
    return ("\n\n[" + " ".join(notes) + "]") if notes else ""



# -- my_requests: what waits for the person ---------------------------------------------

def _age(ts) -> str:
    try:
        minutes = max(0, int((time.time() - float(ts)) // 60))
    except (TypeError, ValueError):
        return ""
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def my_requests_tool(hub_dir: Path):
    """``my_requests``: the approvals and questions waiting for this person on
    this agent, with the Inbox link. Read only: it never decides (the Inbox,
    chat or MCP elicitation do)."""
    from agents import FunctionTool

    from .access import current_identity
    from .hitl import gate, service

    async def invoke(_ctx, _input: str) -> str:
        ident = current_identity()
        if ident is None or not getattr(ident, "user", None):
            return "[my_requests: sign in to see what waits for you.]"
        account, email = getattr(ident, "account_id", None) or ident.user or "", ident.user or ""
        hub_id = gate.hub_id_of(hub_dir)
        try:
            items = await asyncio.to_thread(service.list_for, hub_dir, hub_id, account, email)
        except Exception:  # noqa: BLE001
            log.warning("mcp: my_requests could not read the requests", exc_info=True)
            return "[my_requests: the requests can't be read right now. Try again shortly.]"
        waiting = [service.with_actions(r, account, email) for r in items]
        mine = [r for r in waiting if r.get("allowed_actions")]
        link = service.requests_url()
        if not mine:
            return "Nothing is waiting for you on this agent." + (f" Inbox: {link}" if link else "")
        lines = [f"{len(mine)} waiting for you on this agent:"]
        for r in mine[:20]:
            kind = "Question" if r.get("kind") == "question" else "Approval"
            lines.append(f"- {kind}: {r.get('title') or r.get('tool') or 'a request'}"
                         + (f" ({_age(r.get('created_at'))})" if r.get("created_at") else "")
                         + f" [id {r['id']}]")
        if len(mine) > 20:
            lines.append(f"... and {len(mine) - 20} more.")
        if link:
            lines.append(f"Decide them in the Inbox: {link}")
        return "\n".join(lines)

    return FunctionTool(
        name="my_requests", params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=invoke, strict_json_schema=False,
        description=("List the approvals and questions waiting for the person on this agent, with the "
                     "Inbox link. Read only. Use it when they ask what needs them, or after a call says "
                     "something is waiting."))
