"""FastAPI bridge — exposes an OpenAI-compatible HTTP API in front of a Runtime.

Endpoints:
  GET  /healthz                       liveness probe
  GET  /v1/models                     [{ id, object, created, owned_by }]
  POST /v1/chat/completions           streaming SSE (OpenAI SSE shape) + non-stream

The bridge is built around a single Runtime per process (one hub). The
Runtime is selected based on `MODEL` in <hub>/.env — see `hubzoid/runtime.py`.
Open WebUI, LibreChat, the OpenAI SDK, or curl can all hit /v1/chat/completions
without caring which backend (OpenAI Agents SDK vs Claude Agent SDK) is below.

Run:
  HUBZOID_HUB_DIR=/path/to/hub uvicorn hubzoid.server:app --port 8000

In practice, the CLI calls this in-process; see hubzoid/cli.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import runtime as runtime_lib
from . import settings as settingslib

log = logging.getLogger("hubzoid.server")


def _hub_dir() -> Path:
    p = os.environ.get("HUBZOID_HUB_DIR")
    if not p:
        raise RuntimeError("HUBZOID_HUB_DIR is not set. The bridge needs to know which hub to serve.")
    return Path(p).resolve()


def build_app() -> FastAPI:
    hub_dir = _hub_dir()
    settings = settingslib.load(hub_dir)
    rt = runtime_lib.build(hub_dir)

    # Model label shown in /v1/models. Falls back to the runtime's name
    # (which itself defaults to the main agent's name).
    model_label = settings.model_label or _slugify(rt.name)
    api_keys = set(settings.bridge_api_keys)

    app = FastAPI(title=f"hubzoid · {rt.name}", version="0.1.0")

    def _auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
        if token not in api_keys:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "hub": hub_dir.name, "agent": rt.name}

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        _auth(request)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_label, "object": "model", "created": int(time.time()), "owned_by": "hubzoid"}
                ],
            }
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}")

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages[] required")

        prompt = _flatten_messages(messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="empty prompt after flattening messages")

        if bool(body.get("stream", False)):
            return StreamingResponse(
                _stream(rt, prompt, model_label),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        text = await rt.run(prompt)
        return JSONResponse(_blocking_envelope(text, model_label))

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        if not content:
            continue
        if role == "system":
            parts.insert(0, f"[system]\n{content}")
        elif role == "assistant":
            parts.append(f"[assistant]\n{content}")
        else:
            parts.append(f"[user]\n{content}")
    return "\n\n".join(parts).strip()


def _chunk(content_delta: str | None, *, finish_reason: str | None = None, model: str) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content_delta is not None:
        delta["content"] = content_delta
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


async def _stream(rt, prompt: str, model: str) -> AsyncIterator[bytes]:
    # Role chunk first (OpenAI convention).
    first = _chunk("", model=model)
    first["choices"][0]["delta"] = {"role": "assistant", "content": ""}
    yield f"data: {json.dumps(first)}\n\n".encode()

    async for delta in rt.stream(prompt):
        if delta:
            yield f"data: {json.dumps(_chunk(delta, model=model))}\n\n".encode()

    yield f"data: {json.dumps(_chunk(None, finish_reason='stop', model=model))}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _blocking_envelope(text: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"


# Uvicorn entry point when launched as a separate process.
def _factory():
    return build_app()


app = None  # populated when run via `uvicorn hubzoid.server:app --factory` not needed; we use _factory pattern in cli.py
