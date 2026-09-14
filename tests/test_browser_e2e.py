"""End-to-end tests for the shared browser (HUBZOID_BROWSER).

Marked ``e2e_browser`` so they don't run in the default suite. They spawn a
real ``playwright-mcp`` sidecar and drive a real headless browser.

  * Direct-mode cases need Node/``npx`` on PATH (and a Playwright browser, which
    ``npx @playwright/mcp`` fetches on first run).
  * The pooled-mode case additionally needs Docker up (it runs a browserless
    container) and is skipped otherwise.

Run: ``pytest -m e2e_browser``
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess

import pytest

from hubzoid import browser as browserlib
from hubzoid import settings as settingslib
from hubzoid.loaders import mcp as mcp_loader

pytestmark = pytest.mark.e2e_browser


# --- helpers --------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _has_npx() -> bool:
    return shutil.which("npx") is not None


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def _settings(tmp_path, env, monkeypatch):
    for k in (
        "HUBZOID_BROWSER", "HUBZOID_BROWSER_PORT", "HUBZOID_BROWSER_MCP_URL",
        "HUBZOID_BROWSER_CDP_URL", "HUBZOID_BROWSER_MAX_RSS_MB",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return settingslib.load(tmp_path)


def _text(result) -> str:
    """Flatten an MCP CallToolResult to a searchable string."""
    parts = getattr(result, "content", None) or []
    out = []
    for p in parts:
        t = getattr(p, "text", None)
        out.append(t if t is not None else json.dumps(getattr(p, "__dict__", str(p)), default=str))
    return "\n".join(out) or json.dumps(result, default=str)


def _skip_if_browser_missing(text: str) -> None:
    """Direct mode needs the Playwright browser on the host; skip if absent.

    Install with: npx -p @playwright/mcp@<ver> playwright install chromium
    """
    if "is not installed" in text or "playwright install" in text:
        pytest.skip("Playwright browser not installed on host (direct mode); "
                    "run `npx -p @playwright/mcp playwright install chromium`")


def _playwright_server(tmp_path):
    """Build the auto-injected `playwright` MCP server object via the loader."""
    servers = mcp_loader.load_all(tmp_path)
    matches = [s for s in servers if getattr(s, "name", None) == "playwright"]
    assert matches, "loader did not inject a `playwright` MCP server"
    return matches[0]


# --- direct mode (npx, no docker) -----------------------------------------

@pytest.mark.skipif(not _has_npx(), reason="needs Node/npx")
def test_direct_mode_spawns_lists_tools_and_navigates(tmp_path, monkeypatch):
    port = _free_port()
    s = _settings(tmp_path, {"HUBZOID_BROWSER": "true", "HUBZOID_BROWSER_PORT": port}, monkeypatch)
    mgr = browserlib.build(s)

    async def go():
        await mgr.start()
        try:
            server = _playwright_server(tmp_path)
            async with server:
                tools = await server.list_tools()
                names = {t.name for t in tools}
                assert "browser_navigate" in names, names
                assert len(names) >= 10, f"expected the full toolset, got {len(names)}"
                res = await server.call_tool("browser_navigate", {"url": "https://example.com"})
                _skip_if_browser_missing(_text(res))
                assert "Example Domain" in _text(res)
        finally:
            await mgr.stop()

    asyncio.run(go())


@pytest.mark.skipif(not _has_npx(), reason="needs Node/npx")
def test_direct_mode_one_sidecar_serves_two_sessions(tmp_path, monkeypatch):
    """Two concurrent MCP sessions both work off the single spawned sidecar."""
    port = _free_port()
    s = _settings(tmp_path, {"HUBZOID_BROWSER": "true", "HUBZOID_BROWSER_PORT": port}, monkeypatch)
    mgr = browserlib.build(s)

    async def one():
        server = _playwright_server(tmp_path)
        async with server:
            res = await server.call_tool("browser_navigate", {"url": "https://example.com"})
            txt = _text(res)
            _skip_if_browser_missing(txt)
            return "Example Domain" in txt

    async def go():
        await mgr.start()
        try:
            a, b = await asyncio.gather(one(), one())
            assert a and b
        finally:
            await mgr.stop()

    asyncio.run(go())


def test_stop_is_idempotent_and_safe_when_disabled(tmp_path, monkeypatch):
    """A disabled manager starts/stops as a no-op (no sidecar, no error)."""
    s = _settings(tmp_path, {}, monkeypatch)  # HUBZOID_BROWSER unset
    mgr = browserlib.build(s)

    async def go():
        await mgr.start()
        await mgr.stop()
        await mgr.stop()

    asyncio.run(go())


# --- pooled mode (browserless via docker) ---------------------------------

@pytest.mark.skipif(not (_has_npx() and _docker_up()), reason="needs Node/npx AND Docker up")
def test_pooled_mode_enforces_concurrency_queue(tmp_path, monkeypatch):
    """With CONCURRENT=1, a 2nd session queues behind the 1st (the hard limit)."""
    import httpx

    token = "hubzoidtest"
    name = "hubzoid-browser-e2e"
    # Start browserless with a single slot.
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bless_port = _free_port()
    up = subprocess.run(
        ["docker", "run", "-d", "--name", name, "--memory=1500m",
         "-p", f"{bless_port}:3000",
         "-e", "CONCURRENT=1", "-e", "QUEUED=5",
         "-e", f"TOKEN={token}", "-e", "TIMEOUT=60000",
         "ghcr.io/browserless/chromium:latest"],
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start browserless: {up.stderr.strip()}")

    def pressure():
        r = httpx.get(f"http://localhost:{bless_port}/pressure?token={token}", timeout=5)
        return r.json().get("pressure", {})

    try:
        # Wait for browserless to answer.
        for _ in range(60):
            try:
                if httpx.get(f"http://localhost:{bless_port}/json/version?token={token}", timeout=3).status_code == 200:
                    break
            except Exception:
                pass
            import time as _t; _t.sleep(2)
        else:
            pytest.skip("browserless did not become ready")

        port = _free_port()
        s = _settings(tmp_path, {
            "HUBZOID_BROWSER": "true",
            "HUBZOID_BROWSER_PORT": port,
            "HUBZOID_BROWSER_CDP_URL": f"ws://localhost:{bless_port}?token={token}",
        }, monkeypatch)
        mgr = browserlib.build(s)
        mcp_url = f"http://localhost:{port}/mcp"

        async def open_session(stack):
            """Open a raw MCP session against the sidecar and keep it alive."""
            from contextlib import AsyncExitStack  # noqa: F401 (type hint only)
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            streams = await stack.enter_async_context(streamable_http_client(mcp_url))
            read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return session

        async def go():
            from contextlib import AsyncExitStack

            await mgr.start()
            try:
                async with AsyncExitStack() as a_stack:
                    # Session A takes the single browserless slot and HOLDS it
                    # (its browser context lives as long as the session is open).
                    a = await open_session(a_stack)
                    await a.call_tool("browser_navigate", {"url": "https://example.com"})
                    assert pressure().get("running") == 1, pressure()

                    async with AsyncExitStack() as b_stack:
                        b = await open_session(b_stack)
                        # B's navigate opens a 2nd browserless session -> queues
                        # behind A while A holds the one slot.
                        task = asyncio.create_task(
                            b.call_tool("browser_navigate", {"url": "https://example.org"})
                        )
                        try:
                            queued = 0
                            for _ in range(20):  # up to ~10s for B to reach the queue
                                await asyncio.sleep(0.5)
                                p = pressure()
                                queued = max(queued, p.get("queued", 0))
                                if queued >= 1:
                                    break
                            assert queued >= 1, f"expected B to queue, max pressure seen queued={queued}"
                            assert pressure().get("running") == 1
                        finally:
                            task.cancel()
                            try:
                                await task
                            except BaseException:
                                pass
            finally:
                await mgr.stop()

        asyncio.run(go())
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
