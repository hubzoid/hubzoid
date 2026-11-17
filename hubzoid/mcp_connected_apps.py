# Hubzoid MCP server. Apache-2.0 licensed like the rest of the repository.
"""A person's connected apps (Settings connectors) on the hosted MCP surface.

Chat gives each person the connectors they may use, as themselves, on every
turn (``personal_mcp.per_user_servers``). The hosted MCP server serves the
registry it built at startup, so an assistant connected over MCP never saw
those tools while ``connect_account`` told the person they could use them.
Here, on every ``tools/list`` and ``tools/call``:

  * ``per_user_servers`` decides which apps the caller may use, and with which
    credential: their own token (refreshed when due) or the connector's
    shared key. Grants, surfaces and the admin's allow-list stay there.
  * Each app is reached through the client and wrappers the chat runtimes use
    (``runtime.personal_mcp_server``, the approval gate
    ``hitl.gate.guard_mcp_server`` evaluated with the server key and the app's
    own tool name, and the run check ``access.guard.checked_server``).
  * Its tools are offered as ``<server key>__<tool>`` (``my_crm__whoami``),
    labelled with the connector's name. A call is forwarded only for an alias
    found again in the caller's own catalog, to the app's own tool name, after
    the run check, the allow-list and an access-log row (no row, no call). The
    result comes back as the app sent it: content, structured content, error.

Only tool metadata is cached, briefly and per caller and credential, never a
credential or a decision. The hosted transport is stateless and sends no
list-changed notice: after connecting an app, a person reconnects their
assistant to see its tools (``tools/connect_tools`` says so).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

log = logging.getLogger("hubzoid.mcp")

LIST_SECONDS = 10.0      # one app's listing
BUDGET_SECONDS = 20.0    # every app's listing for one tools/list
CONCURRENCY = 4          # apps listed at once for one caller
CACHE_SECONDS = 60.0
CACHE_SIZE = 256

_NAME_MAX = 128          # MCP tool names: 1-128 of A-Z a-z 0-9 _ . -
_INVALID = re.compile(r"[^A-Za-z0-9_.-]")

# (hub, caller, connector, url, allow-list, credential digest) -> (time, tools)
_cache: OrderedDict[tuple, tuple[float, list]] = OrderedDict()


def alias(key: str, tool: str) -> str:
    """The name a connected app's tool is offered under, ``<key>__<tool>``.

    A character MCP names may not hold becomes ``_`` and the name ends in a
    short hash of the app's own name; an overlong name is cut and ends in a
    hash. So two tools never share an alias, and the call always goes to the
    app's own, unaltered name."""
    def digest(value: str, n: int) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:n]

    clean = _INVALID.sub("_", tool)
    if clean != tool:
        clean += "_" + digest(tool, 6)
    name = f"{key}__{clean}"
    if len(name) > _NAME_MAX:
        name = name[:_NAME_MAX - 9] + "_" + digest(f"{key}\0{tool}", 8)
    return name


def may_be_alias(name: str) -> bool:
    """A cheap first look: could ``name`` be a connected app's tool at all?
    Only a name in the caller's catalog is ever forwarded."""
    from .connectors.per_user import KEY_PREFIX

    return isinstance(name, str) and name.startswith(KEY_PREFIX) and "__" in name


def forget() -> None:
    """Drop every cached listing (tests, and a deployment that changed a lot)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------
async def list_tools(hub_dir: Path, ident, reserved: set[str]) -> list:
    """FastMCP tools for the caller's connected apps. ``reserved``: every name
    the hub serves, hidden from this caller or not; an alias never takes one."""
    servers, labels, _ids = await asyncio.to_thread(_servers, hub_dir, ident)
    if not servers:
        return []
    listed = await _list_all(hub_dir, ident, servers)
    out = []
    for name, (srv, tool) in _aliases(listed, reserved).items():
        out.append(_offered(name, labels.get(srv.server_id) or srv.app, tool))
    return out


async def _list_all(hub_dir: Path, ident, servers: list) -> list:
    """``[(server, tools)]`` within the listing budget. An app that fails, or is
    still listing when the budget runs out, is left out of this answer."""
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(srv):
        key = _cache_key(hub_dir, ident, srv)
        tools = _cached(key)
        if tools is None:
            async with gate:
                try:
                    async with asyncio.timeout(LIST_SECONDS):
                        tools = await _fetch(srv)
                except Exception as exc:  # noqa: BLE001 - one app never sinks the list
                    log.warning("mcp: connected app %r not listed (%s)", srv.key, type(exc).__name__)
                    return srv, []
            _remember(key, tools)
        return srv, _allowed(srv, tools)

    tasks = [asyncio.create_task(one(srv)) for srv in servers]
    done, pending = await asyncio.wait(tasks, timeout=BUDGET_SECONDS)
    for task in pending:
        log.warning("mcp: a connected app was still listing after %ss; left out", BUDGET_SECONDS)
        task.cancel()
    return [task.result() for task in tasks if task in done]


async def _fetch(srv) -> list:
    from .runtime import personal_mcp_server

    async with personal_mcp_server(srv) as server:
        return list(await server.list_tools())


def _allowed(srv, tools: list) -> list:
    """The admin's allow-list, applied again to cached metadata, and only tools
    an assistant can call (a name, an object input schema)."""
    return [t for t in tools
            if isinstance(getattr(t, "name", None), str) and t.name
            and isinstance(getattr(t, "inputSchema", None), dict)
            and (not srv.allowed_tools or t.name in srv.allowed_tools)]


def _aliases(listed: list, reserved: set[str]) -> dict[str, tuple]:
    """``{alias: (server, tool)}``. A name the hub serves stays the hub's, and
    an alias two tools would share is offered for neither."""
    out: dict[str, tuple] = {}
    shared: set[str] = set()
    for srv, tools in listed:
        for tool in tools:
            name = alias(srv.key, tool.name)
            if name in reserved:
                log.warning("mcp: connected app tool %r left out: the hub serves that name", name)
            elif name in shared or name in out:
                log.warning("mcp: connected app tool %r is ambiguous; left out", name)
                out.pop(name, None)
                shared.add(name)
            else:
                out[name] = (srv, tool)
    return out


def _offered(name: str, label: str, tool):
    """The FastMCP tool an assistant sees: the app's input schema and
    annotations, its description labelled with the connector's name
    (provenance, not trust).

    Not the app's output schema: the hub sometimes answers for the app (an
    approval waiting, a refusal), and a declared output schema makes every
    answer without matching structured output an error. The app's own
    structured content is still forwarded whenever it sends some."""
    description = " ".join((tool.description or "").split()) or tool.name
    return _tool_class()(
        name=name, title=getattr(tool, "title", None), description=f"{label}: {description}",
        parameters=tool.inputSchema, annotations=getattr(tool, "annotations", None),
    )


_tool_cls: Any = None


def _tool_class():
    """A listed-only FastMCP Tool: calls arrive through the server's middleware
    (``mcp_server``), which answers them with :func:`call`."""
    global _tool_cls
    if _tool_cls is None:
        from fastmcp.tools import Tool
        from fastmcp.tools.tool import ToolResult

        class _ConnectedAppTool(Tool):
            async def run(self, arguments: dict[str, Any]) -> ToolResult:
                return ToolResult(content=f"[{self.name} is answered by the hub's MCP middleware]",
                                  is_error=True)

        _tool_cls = _ConnectedAppTool
    return _tool_cls


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------
async def call(hub_dir: Path, ident, name: str, arguments: dict | None):
    """Forward ``name`` to the caller's connected app, as them. Returns a FastMCP
    ``ToolResult``, or None when ``name`` is not one of the caller's connected-app
    tools (the server then answers as for any unknown name). ``ident`` is the
    admitted run identity, bound by the caller."""
    from .access import audit
    from .access.evaluator import check_run
    from .access.guard import refusal

    servers, labels, ids = await asyncio.to_thread(_servers, hub_dir, ident)
    candidates = [srv for srv in servers if name.startswith(srv.key + "__")]
    if not candidates:
        await _refused_connector(hub_dir, ident, name, ids)
        return None
    async with AsyncExitStack() as stack:
        found = []
        for srv in candidates:
            server = _client(hub_dir, srv)
            try:
                await stack.enter_async_context(server)
                tools = _allowed(srv, await server.list_tools())
            except Exception as exc:  # noqa: BLE001
                _cache.pop(_cache_key(hub_dir, ident, srv), None)
                log.warning("mcp: connected app %r unavailable (%s)", srv.key, type(exc).__name__)
                continue
            found += [(srv, server, t) for t in tools if alias(srv.key, t.name) == name]
        if len(found) != 1:
            if found:
                log.warning("mcp: %r names more than one connected app tool; not forwarded", name)
            return None
        srv, server, tool = found[0]
        label = labels.get(srv.server_id) or srv.app
        decision = await asyncio.to_thread(check_run, hub_dir, ident, tool=name, connector=srv.server_id)
        refused = refusal(decision, name)
        if refused is not None:
            await asyncio.to_thread(audit.record, hub_dir, user=ident.user, surface=ident.surface,
                                    tool=name, decision="deny", reason=decision.reason)
            return _error(refused)
        recorded = await asyncio.to_thread(audit.record, hub_dir, user=ident.user, surface=ident.surface,
                                           tool=name, decision="allow", reason=f"connector_{srv.server_id}")
        if not recorded:
            return _error(f"[access denied: '{name}' could not be recorded in the access log, "
                          "so it was not run. Try again shortly.]")
        try:
            result = await server.call_tool(tool.name, arguments or {})
        except Exception as exc:  # noqa: BLE001 - never replayed: the outcome is unknown
            _cache.pop(_cache_key(hub_dir, ident, srv), None)
            log.warning("mcp: connected app %r call failed (%s)", srv.key, type(exc).__name__)
            return _error(f"[{name}: {label} did not answer ({type(exc).__name__}). It was not "
                          f"retried, so check in {label} whether it took effect.]")
    return _result(result)


async def _refused_connector(hub_dir: Path, ident, name: str, ids: set[str]) -> None:
    """A name that belongs to a connector this hub has but the caller may not
    use (no grant, not connected, not offered here): logged as a denial."""
    from .access import audit
    from .connectors.per_user import server_key

    if any(name.startswith(server_key(cid) + "__") for cid in ids):
        await asyncio.to_thread(audit.record, hub_dir, user=ident.user, surface=ident.surface,
                                tool=name, decision="deny", reason="connector")


def _client(hub_dir: Path, srv):
    """The chat runtimes' client for one app, with the run check outside the
    approval gate (``runtime.open_personal_mcp``)."""
    from .access.guard import checked_server
    from .hitl import gate as hitl_gate
    from .runtime import personal_mcp_server

    return checked_server(hitl_gate.guard_mcp_server(personal_mcp_server(srv), hub_dir), hub_dir)


def _result(result):
    from fastmcp.tools.tool import ToolResult

    return ToolResult(content=list(getattr(result, "content", None) or []),
                      structured_content=getattr(result, "structuredContent", None),
                      meta=getattr(result, "meta", None),
                      is_error=bool(getattr(result, "isError", False)))


def _error(text: str):
    from fastmcp.tools.tool import ToolResult

    return ToolResult(content=text, is_error=True)


# ---------------------------------------------------------------------------
# The caller's apps, and the metadata cache
# ---------------------------------------------------------------------------
def _servers(hub_dir: Path, ident) -> tuple[list, dict[str, str], set[str]]:
    """(the caller's servers, connector names by ID, every connector ID). Blocking
    (the store, a token refresh): run it in a thread. Empty on any failure."""
    from . import personal_mcp
    from .connectors import registry

    try:
        servers = personal_mcp.per_user_servers(hub_dir, ident)
    except Exception:  # noqa: BLE001 - a store or token hiccup must not break MCP
        log.warning("mcp: connected apps unavailable for this request", exc_info=True)
        return [], {}, set()
    try:
        connectors = registry.list_all(hub_dir)
    except Exception:  # noqa: BLE001 - labels and denial logging only
        connectors = []
    return servers, {c.id: c.name for c in connectors}, {c.id for c in connectors}


def _cache_key(hub_dir: Path, ident, srv) -> tuple:
    """Scoped to the hub, the caller, the connector's address and allow-list,
    and a digest of the credential: a refreshed or new token, a reconnect or a
    changed connector lists again. The credential itself is never kept."""
    credential = hashlib.sha256(repr(sorted(srv.headers.items())).encode()).hexdigest()
    return (str(Path(hub_dir).resolve()), (getattr(ident, "user", None) or "").lower(),
            srv.server_id, srv.url, srv.allowed_tools, credential)


def _cached(key: tuple) -> list | None:
    hit = _cache.get(key)
    if hit is None or time.monotonic() - hit[0] > CACHE_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return hit[1]


def _remember(key: tuple, tools: list) -> None:
    _cache[key] = (time.monotonic(), tools)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
