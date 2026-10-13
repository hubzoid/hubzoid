"""Both real chat interfaces through the public edge; scripted model, no provider.

Release CI runs this once explicitly, irrespective of historical durations.
Each mode gets the same sign-in, streaming and restart-persistence checks.
"""
from __future__ import annotations

import importlib.util
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.chat_helpers import chat_body, events, reply_text

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]
REPO = Path(__file__).resolve().parents[1]
EMAIL, PASSWORD = "release@example.com", "release-smoke-password-123"
PROMPT = "Release smoke canary"


@pytest.mark.parametrize("mode", ["hubzoid", "openwebui"])
def test_real_interface_signin_stream_and_restart(tmp_path, mode):
    if mode == "openwebui" and importlib.util.find_spec("open_webui") is None:
        if os.environ.get("CI"):
            pytest.fail("Release smoke requires the openwebui extra")
        pytest.skip("Install the openwebui extra for the live interface check")
    # Reserve distinct ports together, then release immediately before startup.
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        edge, bridge, webui = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    hub = tmp_path / "smoke"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: smoke\ndescription: Release smoke\n---\nHelp with work.\n")
    base = f"http://127.0.0.1:{edge}"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "USER")}
    env.update(PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
               PYTHONPATH=str(REPO), MODEL="hubzoid-test/scripted", HUBZOID_TEST_RUNTIME="1",
               HUBZOID_UI=mode, HUBZOID_AUTH="true", WEBUI_AUTH="true",
               HUBZOID_ADMIN_EMAIL=EMAIL, HUBZOID_ADMIN_PASSWORD=PASSWORD,
               WEBUI_ADMIN_EMAIL=EMAIL, WEBUI_ADMIN_PASSWORD=PASSWORD,
               WEBUI_SECRET_KEY="release-smoke-only-not-a-real-secret",
               HUBZOID_PUBLIC_URL=base, WEBUI_URL=base, HUBZOID_OWUI_PORT=str(webui),
               BRIDGE_API_KEYS="release-smoke-bridge", MCP_SERVER="false",
               HUBZOID_DISABLE_SCHEDULE="true", LITELLM_LOCAL_MODEL_COST_MAP="True",
               ENABLE_TITLE_GENERATION="false", ANONYMIZED_TELEMETRY="false")
    log_path = tmp_path / "service.log"

    @contextmanager
    def running():
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "hubzoid", "run", str(hub), "--port", str(edge),
                 "--bridge-port", str(bridge), "--no-open"],
                cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=base, timeout=20, headers={"Origin": base}) as client:
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        assert proc.poll() is None, log_path.read_text()[-6000:]
                        try:
                            if client.get("/").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.2)
                    else:
                        pytest.fail("Interface startup timed out: " + log_path.read_text()[-6000:])
                    yield client
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    login = "/api/auth/login" if mode == "hubzoid" else "/api/v1/auths/signin"
    listing = "/api/conversations" if mode == "hubzoid" else "/api/v1/chats/"

    def sign_in(client):
        assert client.get(listing).status_code in (401, 403)
        denied = client.post(login, json={"email": EMAIL, "password": "incorrect"})
        assert denied.status_code in (400, 401, 403), denied.text
        result = client.post(login, json={"email": EMAIL, "password": PASSWORD})
        assert result.status_code == 200, result.text
        if mode == "openwebui":
            client.headers["Authorization"] = "Bearer " + result.json()["token"]
        page = client.get("/")
        assert page.status_code == 200 and "<html" in page.text.lower()
        assets = re.findall(r'(?:src|href)="([^\"]+\.js)"', page.text)
        assert assets, "Chat JavaScript missing"
        assert client.get(assets[0]).status_code == 200
        assert client.get("/portal/").status_code == 200

    with running() as client:
        sign_in(client)
        if mode == "hubzoid":
            agents = client.get("/api/agents").json()["agents"]
            response = client.post("/api/chat", json=chat_body(
                "c_smoke0001", PROMPT, agent=agents[0]["id"],
                message_id="m_smoke0001", assistant_id="m_smoke0002"))
            assert response.status_code == 200, response.text
            answer = reply_text(events(response.text))
            assert PROMPT in answer, response.text
            saved_path = "/api/conversations/c_smoke0001"
        else:
            models = client.get("/api/models").json()["data"]
            response = client.post("/api/chat/completions", json={
                "model": models[0]["id"], "stream": True,
                "messages": [{"role": "user", "content": PROMPT}]})
            assert response.status_code == 200, response.text
            chunks = events(response.text)
            answer = "".join(c.get("choices", [{}])[0].get("delta", {}).get("content", "")
                             for c in chunks if isinstance(c, dict) and c.get("choices"))
            assert PROMPT in answer, response.text
            # Open WebUI's client saves the conversation after receiving the stream.
            saved = client.post("/api/v1/chats/new", json={"chat": {
                "title": PROMPT, "models": [models[0]["id"]],
                "messages": [{"role": "user", "content": PROMPT},
                             {"role": "assistant", "content": answer}]}})
            assert saved.status_code == 200, saved.text
            saved_path = "/api/v1/chats/" + saved.json()["id"]
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "[DONE]" in response.text
        assert answer != PROMPT  # a saved user prompt alone is not a saved reply

        def assert_saved_turn(client):
            saved = client.get(saved_path)
            assert saved.status_code == 200, saved.text
            body = saved.json()
            messages = body["messages"] if mode == "hubzoid" else body["chat"]["messages"]
            turns = [(message["role"],
                      "".join(part["text"] for part in message["content"] if part["type"] == "text")
                      if mode == "hubzoid" else message["content"])
                     for message in messages]
            assert turns == [("user", PROMPT), ("assistant", answer)], turns
            if mode == "hubzoid":
                assert messages[1]["status"] == "complete"

        assert_saved_turn(client)

    # Same data directory, new processes and fresh login, for both interfaces.
    with running() as client:
        sign_in(client)
        assert_saved_turn(client)
