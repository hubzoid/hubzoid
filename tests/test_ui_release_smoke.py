"""The real web app through the public edge; scripted model, no provider.

Release CI runs this once explicitly, irrespective of historical durations:
sign-in, streaming and restart persistence.
"""
from __future__ import annotations

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


def test_real_interface_signin_stream_and_restart(tmp_path):
    # Reserve distinct ports together, then release immediately before startup.
    sockets = [socket.socket() for _ in range(2)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        edge, bridge = [sock.getsockname()[1] for sock in sockets]
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
               HUBZOID_AUTH="true", HUBZOID_MFA_REQUIRED="none",  # two-step sign-in has its own tests
               HUBZOID_ADMIN_EMAIL=EMAIL, HUBZOID_ADMIN_PASSWORD=PASSWORD,
               HUBZOID_PUBLIC_URL=base,
               BRIDGE_API_KEYS="release-smoke-bridge", MCP_SERVER="false",
               HUBZOID_DISABLE_SCHEDULE="true", LITELLM_LOCAL_MODEL_COST_MAP="True")
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

    def sign_in(client):
        assert client.get("/api/conversations").status_code in (401, 403)
        denied = client.post("/api/auth/login", json={"email": EMAIL, "password": "incorrect"})
        assert denied.status_code in (400, 401, 403), denied.text
        result = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        assert result.status_code == 200, result.text
        page = client.get("/")
        assert page.status_code == 200 and "<html" in page.text.lower()
        assets = re.findall(r'(?:src|href)="([^\"]+\.js)"', page.text)
        assert assets, "Chat JavaScript missing"
        assert client.get(assets[0]).status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/portal/").status_code == 200  # the old Console address, sent on to Settings

    with running() as client:
        sign_in(client)
        agents = client.get("/api/agents").json()["agents"]
        response = client.post("/api/chat", json=chat_body(
            "c_smoke0001", PROMPT, agent=agents[0]["id"],
            message_id="m_smoke0001", assistant_id="m_smoke0002"))
        assert response.status_code == 200, response.text
        answer = reply_text(events(response.text))
        assert PROMPT in answer, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "[DONE]" in response.text
        assert answer != PROMPT  # a saved user prompt alone is not a saved reply

        def assert_saved_turn(client):
            saved = client.get("/api/conversations/c_smoke0001")
            assert saved.status_code == 200, saved.text
            messages = saved.json()["messages"]
            turns = [(message["role"],
                      "".join(part["text"] for part in message["content"] if part["type"] == "text"))
                     for message in messages]
            assert turns == [("user", PROMPT), ("assistant", answer)], turns
            assert messages[1]["status"] == "complete"

        assert_saved_turn(client)

    # Same data directory, new processes and a fresh login.
    with running() as client:
        sign_in(client)
        assert_saved_turn(client)
