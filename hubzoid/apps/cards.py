"""The card service: present a tool result as a card, and serve its host.

``wrap(hub_dir, registry)`` is called by every runtime after the access guard
is applied (OpenAI Agents, Claude, Codex and the MCP server), so a bound tool
behaves the same everywhere. A hub without ``mcp-apps.yaml`` is untouched.

When a bound tool returns, ``present`` takes the *active* revision's card for
it, checks the result (JSON, size, the card's JSON Schema, the kind's rules)
and, for progress and approval, that the requester can see the work item or
operation it names. It then stores an instance and returns readable text made
from the same data, which is what the model sees. The card itself travels on
``_request_ctx.record_card`` to the runtime adapter, which attaches it to the
``ToolResult`` it emits. Anything that does not fit keeps the tool's own
result and logs why: a card is presentation, never a reason for a tool to fail.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .. import _request_ctx, access
from ..work_items.errors import WorkItemError, not_found
from . import config as config_lib
from . import kinds, store

log = logging.getLogger(__name__)

APP_STATUS_TOOL = "hub_app_status"
#: Tools a card may call through the host are read tools: a card is never a
#: way to change something (an approval card links to Hub instead).
CALLABLE_CLASSES = ("read",)

_PROGRESS = {"accepted": "pending", "running": "running", "waiting": "waiting", "needs_input": "waiting",
             "completed": "succeeded", "failed": "failed", "cancelled": "canceled", "stopped": "stopped"}
_APPROVAL = {"pending": "approved, not started", "awaiting_approval": "waiting for approval",
             "sending": "executing", "succeeded": "succeeded", "failed_before_send": "failed before sending",
             "rejected": "denied", "ambiguous": "outcome uncertain", "resolved_done": "resolved as done",
             "abandoned": "abandoned"}


def hub_key(hub_dir) -> str:
    return Path(hub_dir).name.lower()


def has_config(hub_dir) -> bool:
    return (Path(hub_dir) / config_lib.FILE).is_file()


def active(hub_dir, *, fresh: bool = False) -> tuple[dict | None, config_lib.Resolved | None]:
    """(active revision row, its configuration), or (None, None)."""
    hub = hub_key(hub_dir)
    row = store.active_row(hub_dir, hub, fresh=fresh)
    if row is None:
        return None, None
    return row, config_lib.from_stored(row["config"], lambda sha: store.resource(hub_dir, hub, sha))


def card_for_tool(hub_dir, tool: str) -> tuple[dict, config_lib.Resolved, config_lib.Card] | None:
    """(active revision, its configuration, the card bound to ``tool``) or None."""
    row, res = active(hub_dir)
    if res is None or not res.enabled:
        return None
    for card in res.cards.values():
        if card.tool == tool:
            return row, res, card
    return None


# -- presenting a result ----------------------------------------------------------

#: Off while a card frame calls a tool: the frame wants the checked data for
#: the card it already shows, not a new card.
_presenting: ContextVar[bool] = ContextVar("hubzoid_cards_presenting", default=True)


@contextmanager
def raw_results() -> Iterator[None]:
    token = _presenting.set(False)
    try:
        yield
    finally:
        _presenting.reset(token)


def wrap(hub_dir, registry: dict) -> dict:
    """``registry`` with every tool able to show its card. Unchanged when the
    hub has no ``mcp-apps.yaml``."""
    if not has_config(hub_dir):
        return registry
    return {name: _wrapped(Path(hub_dir), ft) for name, ft in registry.items()}


def _wrapped(hub_dir: Path, ft):
    original = ft.on_invoke_tool

    async def on_invoke_tool(ctx, args_json):
        result = await original(ctx, args_json)
        if not _presenting.get():
            return result
        try:
            shown = await asyncio.to_thread(present, hub_dir, ft.name, args_json, result,
                                            call_id=getattr(ctx, "tool_call_id", None))
        except Exception:  # noqa: BLE001 - presentation must never break the tool
            log.exception("cards: could not present %s", ft.name)
            return result
        return result if shown is None else shown

    new = copy.copy(ft)
    new.on_invoke_tool = on_invoke_tool
    return new


def _skip(tool: str, code: str, detail: str = "") -> None:
    log.warning("cards: %s shows text, not its card (%s)%s", tool, code, f": {detail}" if detail else "")


def check_result(hub_dir, card: config_lib.Card, result: Any, limits: config_lib.Limits) -> dict:
    """The checked result of ``card``'s tool. Raises ``kinds.Invalid``."""
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except ValueError:
            raise kinds.Invalid("The tool did not return JSON.") from None
    else:
        value = result
    try:
        size = len(json.dumps(value, allow_nan=False))
    except ValueError:
        raise kinds.Invalid("The result has a number that is not finite (NaN or infinity).") from None
    except TypeError:
        raise kinds.Invalid("The result is not plain JSON.") from None
    if size > limits.result_bytes:
        raise kinds.Invalid(f"The result is {size} bytes; the limit is {limits.result_bytes}.")
    if card.schema is not None:
        import jsonschema

        errors = sorted(jsonschema.Draft202012Validator(card.schema).iter_errors(value), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            raise kinds.Invalid(f"does not match the output schema: {e.message[:200]}",
                                "".join(f"/{p}" for p in e.path))
    if card.kind == "custom":
        if not isinstance(value, dict):
            raise kinds.Invalid("The result must be a JSON object.")
        return value
    return kinds.check(card.kind, value, rows_per_page=limits.rows_per_page)


def status(hub_dir, checked: dict, viewer: str) -> dict | None:
    """The authoritative state of a progress or approval card for ``viewer``
    (raises not_found when they cannot see what it names)."""
    from ..work_items import service as wi

    hub = hub_key(hub_dir)
    if checked["kind"] == "progress":
        item = wi.show(hub_dir, checked["work_item_id"], viewer)
        return {"state": item["state"], "label": _PROGRESS.get(item["state"], item["state"]),
                "summary": item.get("reason") or "", "link": link(hub, item["id"]),
                "facts": {"workflow": item["workflow"], "requester": item["requester"],
                          "updated": item["updated"]}}
    if checked["kind"] == "approval":
        op = wi.operation_detail(hub_dir, checked["operation_id"], viewer)
        changed = op.get("args_digest") != checked["digest"]
        label = "changed since this card was made" if changed else _APPROVAL.get(op["state"], op["state"])
        return {"state": "changed" if changed else op["state"], "label": label, "summary": "",
                "link": link(hub, op["item_id"]),
                "facts": {"action": op.get("action") or op.get("name"), "operation": op["name"],
                          "requester": _op_requester(hub_dir, op), "digest": op.get("args_digest"),
                          "created": op.get("created"), "approval": op.get("approval")}}
    return None


def _op_requester(hub_dir, op: dict) -> str | None:
    from ..work_items import store as wi_store

    item = wi_store.get_item(hub_dir, op["item_id"])
    return (item or {}).get("requester")


def link(hub: str, item_id: str) -> str:
    """Where a person acts on a work item: its page in Hub. Absolute when the
    deployment has a public URL (an external host needs one), else a path."""
    from .. import appmode

    base = appmode.public_url()
    if not base:
        mcp_url = (os.environ.get("MCP_PUBLIC_URL") or "").strip()
        base = appmode.normalize_origin(mcp_url) if mcp_url else ""
    return f"{base}/settings#/work/{hub}/{item_id}"


_ARGS_MAX = 20_000


def present(hub_dir: Path, tool: str, args_json: str, result: Any, *, call_id: str | None = None) -> str | None:
    """The text the model sees for ``tool``'s result when it shows a card, else
    None (keep the tool's own result)."""
    found = card_for_tool(hub_dir, tool)
    if found is None:
        return None
    if isinstance(result, str) and result.startswith("[access denied"):
        return None
    row, res, card = found
    try:
        checked = check_result(hub_dir, card, result, res.limits)
    except kinds.Invalid as exc:
        _skip(tool, "output_schema_mismatch", f"{exc.pointer} {exc.message}".strip())
        return None
    ident = access.current_identity()
    requester = (ident.user or "").strip().lower() or "anonymous"
    state = None
    if card.kind in ("progress", "approval"):
        try:
            state = status(hub_dir, checked, requester)
        except WorkItemError as exc:
            _skip(tool, exc.code)
            return None
    hub = hub_key(hub_dir)
    text = kinds.text(checked, status=state, link=(state or {}).get("link")) if card.kind != "custom" \
        else _custom_text(checked)
    try:
        args = json.loads(args_json) if isinstance(args_json, str) and args_json else {}
    except ValueError:
        args = {}
    args = args if isinstance(args, dict) else {}
    args_text = json.dumps(args, default=str)
    if len(args_text) > _ARGS_MAX:       # kept whole or not at all: a cut would not be JSON
        args, args_text = None, "null"
    instance = store.create_instance(hub_dir, {
        "id": str(uuid.uuid4()), "hub": hub, "card_id": card.id, "kind": card.kind, "server_id": card.server_id,
        "tool": tool, "config_revision": row["revision"], "resource_sha256": card.sha256,
        "requester": requester, "surface": ident.surface, "conversation_id": _request_ctx.get_chat_id(),
        "arguments_json": args_text, "result_json": json.dumps(checked), "text": text})
    _request_ctx.record_card(text, envelope(hub, card, instance["id"], checked, text, arguments=args),
                             call_id=call_id)
    return text


def _custom_text(checked: dict) -> str:
    title = checked.get("title") if isinstance(checked.get("title"), str) else None
    body = checked.get("text") if isinstance(checked.get("text"), str) else None
    return body or (title or json.dumps(checked, default=str)[:4000])


def envelope(hub: str, card: config_lib.Card, instance_id: str, checked: dict, text: str, *,
             arguments: dict | None = None) -> dict:
    """What the chat stores on the tool part: an MCP ``CallToolResult`` plus
    the card's identity, what it may call, and the call's own arguments (for
    its next page; None when they were too long to keep). Small, JSON only."""
    return {"instance_id": instance_id, "card_id": card.id, "kind": card.kind, "title": card.title,
            "tool": card.tool, "server_id": card.server_id, "resource_uri": card.uri(hub), "text": text,
            "structured": checked, "allowed_tools": list(card.allowed_tools), "arguments": arguments}


def call_tool_result(env: dict) -> dict:
    """An envelope as the MCP Apps ``CallToolResult`` a card receives."""
    meta = {k: env.get(k) for k in ("instance_id", "card_id", "kind", "tool", "resource_uri", "allowed_tools")}
    if env.get("arguments") is not None:
        meta["arguments"] = env["arguments"]
    return {"content": [{"type": "text", "text": env["text"]}], "structuredContent": env["structured"],
            "_meta": {"hubzoid/card": meta}}


# -- instances -------------------------------------------------------------------

def _viewer_owns(instance: dict, viewer: str) -> bool:
    return viewer.startswith("cli:") or instance["requester"] == (viewer or "").strip().lower()


def instance(hub_dir, instance_id: str, viewer: str) -> dict:
    """The instance as its requester sees it (anyone else gets not_found)."""
    hub = hub_key(hub_dir)
    inst = store.get_instance(hub_dir, hub, instance_id)
    if not _viewer_owns(inst, viewer):
        raise not_found("card")
    return inst


def granted(hub_dir, inst: dict, card) -> list[str]:
    """The tools a card instance may call now: allowed when it was made AND
    allowed today. An older instance never gains a tool added later."""
    if card is None:
        return []
    then = store.revision_config(hub_dir, hub_key(hub_dir), inst["config_revision"]) or {}
    before = next((c for c in then.get("cards") or [] if c.get("id") == inst["card_id"]), None)
    allowed_then = set((before or {}).get("allowed_tools") or [])
    return [t for t in card.allowed_tools if t in allowed_then]


def view(hub_dir, instance_id: str, viewer: str) -> dict:
    """The safe projection: result, text, current state, binding and what the
    card may still do."""
    inst = instance(hub_dir, instance_id, viewer)
    row, res = active(hub_dir, fresh=True)
    card = (res.cards.get(inst["card_id"]) if res else None)
    live = card is not None and res.enabled and card.sha256 == inst["resource_sha256"] and not inst["revoked"]
    out = {"id": inst["id"], "card_id": inst["card_id"], "kind": inst["kind"], "tool": inst["tool"],
           "server_id": inst["server_id"], "requester": inst["requester"], "created": inst["created"],
           "config_revision": inst["config_revision"], "resource_sha256": inst["resource_sha256"],
           "resource_uri": f"ui://{hub_key(hub_dir)}/{inst['card_id']}/{inst['resource_sha256']}/index.html",
           "result": inst["result"], "text": inst["text"], "revoked": inst["revoked"],
           "revoke_reason": inst["revoke_reason"],
           "actions": (granted(hub_dir, inst, card) if live else []), "live": live,
           "inactive_reason": None if live else _inactive(inst, card, res)}
    if inst["kind"] in ("progress", "approval"):
        try:
            out["status"] = status(hub_dir, inst["result"], viewer)
        except WorkItemError as exc:
            out["status"] = {"state": "unavailable", "label": "not available", "error": exc.code}
    return out


def _inactive(inst: dict, card, res) -> str:
    if inst["revoked"]:
        return "revoked"
    if res is None or not res.enabled:
        return "cards_disabled"
    if card is None:
        return "card_removed"
    return "resource_changed"


def revoke(hub_dir, instance_id: str, actor: str, reason: str) -> dict:
    inst = instance(hub_dir, instance_id, actor)
    if not inst["revoked"]:
        store.revoke(hub_dir, hub_key(hub_dir), inst["id"], actor=actor, reason=reason or "revoked")
        _audit(hub_dir, actor, "apps.revoke", inst["id"], {"card": inst["card_id"], "reason": reason[:200]})
    return view(hub_dir, instance_id, actor)


def _audit(hub_dir, actor: str, action: str, subject: str, detail: dict) -> None:
    from ..control import audit

    try:
        with store.engine(hub_dir).begin() as conn:
            audit.write(conn, actor, action, subject=subject, hub=hub_key(hub_dir), detail=detail,
                        surface="apps")
    except Exception:  # noqa: BLE001 - the audit row is best effort, the refusal is not
        log.exception("cards: could not write the %s audit row", action)


# -- the host: what a card frame may ask for ----------------------------------

_rate: dict[tuple[str, str], list[float]] = {}
_inflight: dict[tuple[str, str], int] = {}


def _limited(key: tuple[str, str], limits: config_lib.Limits) -> None:
    now = time.monotonic()
    calls = [t for t in _rate.get(key, []) if now - t < 60]
    if len(calls) >= limits.calls_per_minute:
        raise WorkItemError("rate_limited", "This card made too many calls. Wait a minute and try again.",
                            status=429, retry_after=int(60 - (now - calls[0])) + 1)
    if _inflight.get(key, 0) >= limits.concurrent_calls:
        raise WorkItemError("rate_limited", "This card has too many calls running. Try again shortly.",
                            status=429, retry_after=2)
    calls.append(now)
    _rate[key] = calls


def limit(hub_dir, viewer: str, inst: dict) -> None:
    """The per-card, per-person limits, for a request that is not a dispatch
    (the MCP status refresh)."""
    _row, res = active(hub_dir)
    _limited((viewer, inst["id"]), res.limits if res else config_lib.Limits())


def retained_html(hub_dir, card_id: str, sha256: str) -> str | None:
    """An earlier version of ``card_id``'s HTML that is still kept, or None."""
    row = store.resource_row(hub_dir, hub_key(hub_dir), sha256)
    return row["html"] if row and row["card_id"] == card_id else None


def resource_for(hub_dir, inst: dict) -> dict:
    """``mcp-apps/read-resource``: the instance's own HTML, for assistant-ui."""
    hub = hub_key(hub_dir)
    html = store.resource(hub_dir, hub, inst["resource_sha256"])
    if html is None:
        raise not_found("card resource")
    uri = f"ui://{hub}/{inst['card_id']}/{inst['resource_sha256']}/index.html"
    return {"uri": uri, "mimeType": config_lib.MIME, "html": html, "meta": resource_meta()}


def resource_meta() -> dict:
    """``_meta.ui`` of every card resource: no network, no external assets."""
    return {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
            "prefersBorder": True}


async def dispatch(hub_dir, instance_id: str, viewer: str, method: str, params: dict, *,
                   identity, call_tool) -> Any:
    """One request from a card frame, rechecked now. ``call_tool(name, args)``
    runs a hub tool as ``identity`` through the normal guards (it returns text).
    Widget-supplied hub, server or caller ids are refused, never trusted."""
    params = params if isinstance(params, dict) else {}
    for key in ("hub", "hubId", "caller", "user", "server_url"):
        if key in params:
            raise WorkItemError("invalid_request", f"{key} is not accepted from a card.")
    inst = await asyncio.to_thread(instance, hub_dir, instance_id, viewer)
    if inst["revoked"]:
        raise WorkItemError("revoked", "This card was revoked. Ask the agent again for a current one.", status=410)
    # Authorization reads the active revision now, never a cached one.
    row, res = await asyncio.to_thread(active, hub_dir, fresh=True)
    card = res.cards.get(inst["card_id"]) if res else None
    key = (viewer, inst["id"])
    _limited(key, res.limits if res else config_lib.Limits())
    uri = f"ui://{hub_key(hub_dir)}/{inst['card_id']}/{inst['resource_sha256']}/index.html"
    if method in ("mcp-apps/read-resource", "resources/read"):
        if params.get("uri") not in (None, uri):
            raise WorkItemError("forbidden", "A card can read only its own resource.", status=403)
        res_out = await asyncio.to_thread(resource_for, hub_dir, inst)
        if method == "mcp-apps/read-resource":
            return res_out
        return {"contents": [{"uri": uri, "mimeType": config_lib.MIME, "text": res_out["html"],
                              "_meta": {"ui": resource_meta()}}]}
    if method == "resources/list":
        return {"resources": [{"uri": uri, "name": inst["card_id"], "mimeType": config_lib.MIME}]}
    if method != "tools/call":
        raise WorkItemError("method_not_allowed", f"{method} is not available to cards.", status=405)
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise WorkItemError("invalid_request", "arguments must be an object.")
    if name == APP_STATUS_TOOL:
        # A read of the state the card names: allowed for an older version too.
        return await asyncio.to_thread(_status_result, hub_dir, inst, viewer)
    if card is None or not res.enabled or card.sha256 != inst["resource_sha256"]:
        raise WorkItemError("card_inactive", "This card is an older version. Ask the agent again for a current "
                            "one.", status=409)
    if name not in await asyncio.to_thread(granted, hub_dir, inst, card):
        await asyncio.to_thread(_audit, hub_dir, viewer, "apps.denied", inst["id"],
                                {"tool": str(name)[:100], "reason": "not_allowed"})
        raise WorkItemError("forbidden", "This card may not call that tool.", status=403)
    _inflight[key] = _inflight.get(key, 0) + 1
    try:
        outcome = await call_tool(name, args, identity, CALLABLE_CLASSES)
    finally:
        _inflight[key] -= 1
    if outcome.get("refused"):
        await asyncio.to_thread(_audit, hub_dir, viewer, "apps.denied", inst["id"],
                                {"tool": name, "reason": outcome["refused"]})
        raise WorkItemError("forbidden", outcome["message"], status=403)
    text = outcome["text"]
    if text.startswith("[access denied"):
        await asyncio.to_thread(_audit, hub_dir, viewer, "apps.denied", inst["id"],
                                {"tool": name, "reason": "access_denied"})
        return {"content": [{"type": "text", "text": text}], "isError": True}
    target = next((c for c in res.cards.values() if c.tool == name), None)
    if target is not None:
        try:
            checked = check_result(hub_dir, target, text, res.limits)
            shown = kinds.text(checked) if target.kind in ("table", "report") else text
            return {"content": [{"type": "text", "text": shown}], "structuredContent": checked}
        except kinds.Invalid as exc:
            _skip(name, "output_schema_mismatch", exc.message)
    return {"content": [{"type": "text", "text": text}]}


def _status_result(hub_dir, inst: dict, viewer: str) -> dict:
    if inst["revoked"]:
        return {"content": [{"type": "text", "text": "This card was revoked."}], "isError": True}
    try:
        st = status(hub_dir, inst["result"], viewer)
    except WorkItemError as exc:
        return {"content": [{"type": "text", "text": exc.message}], "isError": True}
    if st is None:
        return {"content": [{"type": "text", "text": "This card has no live state."}], "isError": True}
    return {"content": [{"type": "text", "text": kinds.text(inst["result"], status=st, link=st["link"])}],
            "structuredContent": {**inst["result"], "status": st}}


# -- configuration ---------------------------------------------------------------

def tool_names(hub_dir) -> set[str]:
    """The hub's tool names, to check bindings against."""
    from .. import mcp_server

    registry, _ = mcp_server.build_registry(Path(hub_dir))
    return set(registry) | set(mcp_server.CHAT_SCOPED_TOOLS)


def validate(hub_dir, apps_doc: dict | None = None) -> config_lib.Resolved:
    """Check the files (or ``apps_doc`` in place of ``mcp-apps.yaml``)."""
    return config_lib.load(Path(hub_dir), tool_names=tool_names(hub_dir), apps_doc=apps_doc)


def summary(hub_dir, res: config_lib.Resolved) -> dict:
    hub = hub_key(hub_dir)
    return {"valid": res.valid, "present": res.present, "enabled": res.enabled,
            "errors": [p.dict() for p in res.problems], "digest": res.digest() if res.valid else None,
            "limits": res.limits.model_dump(), "sources": res.sources,
            "resolved_resources": [c.public(hub) for c in sorted(res.cards.values(), key=lambda c: c.id)]}


def get(hub_dir) -> dict:
    row, res = active(hub_dir, fresh=True)
    hub = hub_key(hub_dir)
    return {"revision": row["revision"] if row else None, "digest": row["digest"] if row else None,
            "applied_by": row["actor"] if row else None, "applied_at": row["created"] if row else None,
            "enabled": bool(res and res.enabled), "limits": res.limits.model_dump() if res else None,
            "cards": [c.public(hub) for c in sorted(res.cards.values(), key=lambda c: c.id)] if res else [],
            "history": store.history(hub_dir, hub)}


def apply(hub_dir, *, expected: int | None, actor: str, apps_doc: dict | None = None) -> dict:
    """Make the checked configuration the next revision. With ``apps_doc``
    (the API's body), that document is checked in memory and written to
    ``mcp-apps.yaml`` only once its revision is set, so a stale or invalid
    request never changes the file."""
    res = validate(hub_dir, apps_doc)
    if not res.present:
        raise WorkItemError("config_invalid", "There is no mcp-apps.yaml to apply.", status=422)
    if not res.valid:
        raise WorkItemError("config_invalid", "Fix the card configuration, then apply again.", status=422,
                            errors=[p.dict() for p in res.problems])
    out = store.apply(hub_dir, hub_key(hub_dir), expected=expected, digest=res.digest(), config=res.stored(),
                      resources={c.sha256: (c.id, c.html) for c in res.cards.values()}, actor=actor)
    if apps_doc is not None:
        from ..control import yamlfile

        (Path(hub_dir) / config_lib.FILE).write_text(yamlfile.dumps(apps_doc), encoding="utf-8")
    _audit(hub_dir, actor, "apps.apply", f"revision:{out['revision']}", {"digest": out["digest"]})
    return out


def probe(hub_dir, client_capabilities: Any) -> dict:
    """How a client with ``client_capabilities`` would get each card: rendered,
    or as text. A protocol check, never a claim that a host works."""
    caps = client_capabilities if isinstance(client_capabilities, dict) else {}
    ext = (caps.get("extensions") or {}) if isinstance(caps.get("extensions"), dict) else {}
    ui = ext.get("io.modelcontextprotocol/ui")
    mimes = (ui or {}).get("mimeTypes") if isinstance(ui, dict) else None
    renders = isinstance(ui, dict) and (mimes is None or config_lib.MIME in mimes)
    row, res = active(hub_dir, fresh=True)
    cards = []
    for card in (res.cards.values() if res else []):
        cards.append({"card_id": card.id, "tool": card.tool, "mode": "card" if renders and res.enabled else "text",
                      "text_fallback": True})
    return {"mode": "card" if renders else "text", "negotiated_extension": "io.modelcontextprotocol/ui"
            if renders else None, "active_revision": row["revision"] if row else None, "cards": cards,
            "note": "A protocol check only. Host support is recorded from a real host, never from this probe."}
