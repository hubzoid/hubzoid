"""Dispatch an inbound message to the hub's OpenAI-compatible bridge.

The same contract every surface converges on: POST ``/v1/chat/completions`` with
``stream=true`` and forward the resolved identity as headers
(``X-OpenWebUI-User-Email`` / ``X-Hubzoid-Groups`` / ``X-Hubzoid-Surface``) —
exactly what the Slack adapter does. Webhook surfaces can't stream-edit a
message, so we collect the whole reply into one string and send it once.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

# A generous default so a stalled bridge/LLM can't hang a worker (a Starlette
# threadpool thread) forever, while still allowing a slow model to stream.
# Applied only when we construct the client ourselves.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


def dispatch(
    *,
    bridge_url: str,
    api_key: str,
    model: str,
    messages: "list[dict[str, str]]",
    surface: str,
    user_email: "str | None" = None,
    groups: "list[str] | None" = None,
    chat_id: "str | None" = None,
    on_delta=None,
    http_client: "httpx.Client | None" = None,
    timeout: "float | None" = None,
) -> str:
    """POST to the bridge and return the full assembled reply text.

    `bridge_url` is the `/v1` base (e.g. http://127.0.0.1:8000/v1). Identity
    headers are sent only when present, so an anonymous surface stays anonymous.
    """
    client = http_client or httpx.Client(timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT)
    owns = http_client is None
    body: "dict[str, Any]" = {"model": model, "messages": messages, "stream": True}
    if chat_id:
        body["chat_id"] = chat_id
    headers = {"Authorization": f"Bearer {api_key}", "X-Hubzoid-Surface": surface}
    if user_email:
        headers["X-OpenWebUI-User-Email"] = user_email
    if groups:
        headers["X-Hubzoid-Groups"] = ",".join(groups)

    parts: "list[str]" = []
    try:
        with client.stream(
            "POST", f"{bridge_url}/chat/completions", headers=headers, json=body,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                delta = parse_sse_delta(line)
                if delta:
                    parts.append(delta)
                    if on_delta is not None:
                        try:
                            on_delta(delta)
                        except Exception:  # noqa: BLE001 — a UI callback never breaks dispatch
                            pass
    finally:
        if owns:
            client.close()
    return "".join(parts)


def parse_sse_delta(line: "bytes | str") -> "str | None":
    """Extract ``choices[0].delta.content`` from one OpenAI-style SSE line.

    Returns None for the ``[DONE]`` sentinel, role-only / finish-only chunks,
    blank lines, non-``data:`` lines, and malformed JSON — matching what
    ``hubzoid.server._stream`` emits.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    s = line.strip()
    if not s or not s.startswith("data:"):
        return None
    payload = s[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        delta = obj["choices"][0].get("delta") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    content = delta.get("content")
    if not isinstance(content, str) or not content:
        return None
    return content
