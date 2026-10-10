"""What a connector offers: the tools its MCP server lists, for the Console.

The Console's Test shows them (as Mastra Studio lists an MCP server's tools and
Sim shows each server's tool count), and the allowed-tools picker offers them.
A small synchronous MCP client over the connectors' guarded HTTP client
(``connectors.http``), so the same address rule applies as for every other
request: ``initialize``, ``notifications/initialized``, ``tools/list`` (paged),
then the session is closed. Only names and short descriptions come back.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from . import ConnectorError
from . import http as net

log = logging.getLogger("hubzoid.connectors")

MAX_TOOLS = 200
MAX_PAGES = 5
MAX_BYTES = 2 * 1024 * 1024
_DESCRIPTION_MAX = 240


def _version() -> str:
    from .discovery import _protocol_version

    return _protocol_version()


def _message(response: httpx.Response, want_id: int, deadline: float) -> dict | None:
    """The JSON-RPC reply with ``want_id``, from a JSON or an SSE body."""
    size, chunks = 0, []
    for chunk in response.iter_bytes():
        if time.monotonic() > deadline:
            raise httpx.ReadTimeout("response took too long", request=response.request)
        size += len(chunk)
        if size > MAX_BYTES:
            return None
        chunks.append(chunk)
        if "text/event-stream" in response.headers.get("content-type", ""):
            found = _from_sse(b"".join(chunks), want_id)
            if found is not None:
                return found
    body = b"".join(chunks)
    if "text/event-stream" in response.headers.get("content-type", ""):
        return _from_sse(body, want_id)
    try:
        value = json.loads(body or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("id") == want_id else None


def _from_sse(body: bytes, want_id: int) -> dict | None:
    for event in body.decode("utf-8", "replace").split("\n\n"):
        data = "".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            value = json.loads(data)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id") == want_id:
            return value
    return None


def list_tools(url: str, headers: dict | None = None, *, deadline_s: float = 20.0) -> list[dict]:
    """``[{name, description}]`` the server at ``url`` lists, with ``headers``
    (a person's Bearer, or a Shared key). Raises ConnectorError: ``unreachable``,
    ``unauthorized`` (the server refused the credential) or ``bad_response``."""
    version = _version()
    base = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version,
            **(headers or {})}
    deadline = time.monotonic() + deadline_s
    tools: list[dict] = []
    session = None
    try:
        with net.client(url) as c:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {},
                               "clientInfo": {"name": "Hubzoid", "version": "1"}}}
            with c.stream("POST", url, json=init, headers=base) as r:
                if r.status_code in (401, 403):
                    raise ConnectorError("unauthorized", "The server refused the credential.", 502)
                if r.status_code >= 400:
                    raise ConnectorError("bad_response", f"The server answered with status "
                                                         f"{r.status_code}.", 502)
                session = r.headers.get("mcp-session-id")
                if _message(r, 1, deadline) is None:
                    raise ConnectorError("bad_response", "The server did not answer initialize.", 502)
            hdrs = {**base, **({"mcp-session-id": session} if session else {})}
            c.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                   headers=hdrs)
            cursor = None
            for page in range(MAX_PAGES):
                params = {"cursor": cursor} if cursor else {}
                req = {"jsonrpc": "2.0", "id": 2 + page, "method": "tools/list", "params": params}
                with c.stream("POST", url, json=req, headers=hdrs) as r:
                    reply = _message(r, 2 + page, deadline) if r.status_code < 400 else None
                result = (reply or {}).get("result")
                if not isinstance(result, dict):
                    raise ConnectorError("bad_response", "The server did not list its tools.", 502)
                for t in result.get("tools") or []:
                    if isinstance(t, dict) and isinstance(t.get("name"), str):
                        desc = t.get("description") if isinstance(t.get("description"), str) else ""
                        tools.append({"name": t["name"][:128],
                                      "description": " ".join(desc.split())[:_DESCRIPTION_MAX]})
                cursor = result.get("nextCursor")
                if not cursor or len(tools) >= MAX_TOOLS:
                    break
            if session:
                try:
                    c.request("DELETE", url, headers=hdrs, timeout=5.0)
                except httpx.HTTPError:
                    pass
    except ConnectorError:
        raise
    except httpx.HTTPError as exc:
        log.info("connectors: tools of %s not listed (%s)", net.origin(url), type(exc).__name__)
        raise ConnectorError("unreachable", "The server could not be reached.", 502) from None
    return tools[:MAX_TOOLS]
