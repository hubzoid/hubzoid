"""Shared set-up for the web app chat tests (tests/test_chat_*.py).

A scratch hub made by ``hubzoid init --model hubzoid-test/scripted``, served by
``server.build_app()`` in-process with the scripted test runtime. The hub's
``.env`` is removed after init and its settings are set with ``monkeypatch``, so
nothing leaks into other tests' environment.

People: local mode (sign-in off) makes every request the local owner. Tests
that need several people replace ``hubzoid.auth.sessions.resolve`` with
``as_people``: the ``X-Test-User`` header names who is signed in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from hubzoid.auth import AuthUser

ORIGIN = {"Origin": "http://testserver"}

PEOPLE = {
    "ana": AuthUser(id="u_ana", email="ana@example.org", name="Ana Example", role="user",
                    method="password"),
    "ben": AuthUser(id="u_ben", email="ben@example.org", name="Ben Example", role="user",
                    method="password"),
}

_CLEAR = ("HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
          "DATABASE_URL", "HUBZOID_DEPLOYMENT", "SHOW_TOOLS", "SHOW_THINKING", "MODEL_LABEL",
          "HUBZOID_ARTIFACT_LINK_TTL", "HUBZOID_ARTIFACT_SECRET", "HUBZOID_MAX_UPLOAD_BYTES",
          "HUBZOID_MAX_FILES_PER_MESSAGE", "HUBZOID_TITLE_MODEL", "HUBZOID_ALLOWED_ORIGINS",
          "HUBZOID_OPERATIONAL_DB", "MCP_SERVER", "HUBZOID_BROWSER")


def make_hub(tmp_path: Path, monkeypatch, name: str = "chat-hub", **env: str) -> Path:
    from typer.testing import CliRunner

    from hubzoid.cli import app as cli_app

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli_app, ["init", name, "--model", "hubzoid-test/scripted"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    hub = tmp_path / name
    (hub / ".env").unlink()
    for key in _CLEAR:
        monkeypatch.delenv(key, raising=False)
    settings = {"HUBZOID_HUB_DIR": str(hub), "MODEL": "hubzoid-test/scripted",
                "HUBZOID_TEST_RUNTIME": "1", "BRIDGE_API_KEYS": "k-chat-test",
                "BRIDGE_PORT": "3301", "HUBZOID_TEST_SLOW_SECONDS": "1"}
    settings.update(env)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    from hubzoid.access import store_for

    for person in PEOPLE.values():  # both may use the agent (Console grants)
        store_for(hub).grant(person.email, hub.name, "use_hub", actor="test")
    return hub


def api_headers(hub_dir, email: str = "ana@example.org") -> dict:
    """An OpenAI-compatible API call for `email`: the bridge key and a signed
    identity (an API call without one is anonymous and is refused)."""
    from hubzoid import assertions

    return {"Authorization": "Bearer k-chat-test",
            **assertions.identity_headers(hub_dir, surface="api", email=email)}


def build_app():
    from hubzoid.server import build_app as build

    return build()


def as_people(monkeypatch) -> None:
    """Sign in whoever the X-Test-User header names (nobody without it)."""
    from hubzoid.auth import sessions

    monkeypatch.setattr(sessions, "resolve",
                        lambda request, hub_dir: PEOPLE.get(request.headers.get("x-test-user", "")))


def who(name: str) -> dict:
    return {"X-Test-User": name, **ORIGIN}


def events(body: str) -> list:
    """The SSE ``data:`` payloads of a response body (JSON, or the string '[DONE]')."""
    out = []
    for block in body.split("\n\n"):
        if block.startswith("data: "):
            payload = block[len("data: "):]
            out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def chat_body(conv_id: str, text: str | None, *, agent: str | None, message_id: str | None = None,
              parent_id: str | None = None, assistant_id: str | None = None,
              files: tuple = ()) -> dict:
    body: dict = {"conversation_id": conv_id, "parent_id": parent_id}
    if agent is not None:
        body["agent"] = agent
    if text is not None or files:
        content = [{"type": "text", "text": text}] if text is not None else []
        content += [{"type": "file", "file_id": f} for f in files]
        body["message"] = {"id": message_id, "content": content}
    if assistant_id:
        body["assistant_message_id"] = assistant_id
    return body


def reply_text(events_: list) -> str:
    return "".join(e["delta"] for e in events_ if isinstance(e, dict) and e.get("type") == "text-delta")


def wait_titles(timeout: float = 10.0) -> None:
    from hubzoid.chat import titles

    assert titles.workers.wait_idle(timeout)
