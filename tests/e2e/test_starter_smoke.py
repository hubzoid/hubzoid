"""End-to-end smoke test: a real LLM call through the bridge against the
starter `hubzoid init` writes.

Requires OPENROUTER_API_KEY in the environment. The test is marked `e2e` and
skipped by default; run with:

    pytest -m e2e
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PORT = 8765  # uncommon, avoids collisions

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def bridge_proc(tmp_path_factory):
    work = tmp_path_factory.mktemp("starter")
    subprocess.run([sys.executable, "-m", "hubzoid.cli", "init", "starter", "--model", "claude-local"],
                   cwd=work, check=True, capture_output=True)
    HUB = work / "starter"
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set; cannot run e2e against OpenRouter")

    env = os.environ.copy()
    env["HUBZOID_HUB_DIR"] = str(HUB)
    env["MODEL"] = env.get("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    env["BRIDGE_API_KEYS"] = "e2e-key"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "hubzoid.server:build_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # Wait for it to come up.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("bridge did not come up in time")

    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _model() -> str:
    """The starter's agent id, as the bridge lists it."""
    r = httpx.get(f"http://127.0.0.1:{PORT}/v1/models", headers={"Authorization": "Bearer e2e-key"}, timeout=10.0)
    return r.json()["data"][0]["id"]


def test_real_chat_completion(bridge_proc):
    r = httpx.post(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        headers={"Authorization": "Bearer e2e-key", "Content-Type": "application/json"},
        json={
            "model": _model(),
            "stream": False,
            "messages": [
                {"role": "user", "content": "Reply with exactly the word: pong"}
            ],
        },
        timeout=60.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    content = body["choices"][0]["message"]["content"].strip().lower()
    # Tolerant: the model occasionally adds punctuation. Just check pong is in it.
    assert "pong" in content, f"expected 'pong' in response, got: {content!r}"


def test_real_chat_streaming(bridge_proc):
    chunks: list[str] = []
    with httpx.stream(
        "POST",
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        headers={"Authorization": "Bearer e2e-key", "Content-Type": "application/json"},
        json={
            "model": _model(),
            "stream": True,
            "messages": [{"role": "user", "content": "In one word: yes or no?"}],
        },
        timeout=60.0,
    ) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            chunks.append(line)
    full = "\n".join(chunks)
    assert "data: [DONE]" in full
    assert "chat.completion.chunk" in full
