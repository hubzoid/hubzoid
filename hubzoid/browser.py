"""Shared, resource-limited Playwright browser for a hub.

When ``HUBZOID_BROWSER=true`` every agent in the hub gets the full Playwright
MCP toolset (``browser_navigate``, ``browser_click``, ``browser_snapshot`` …)
backed by ONE shared browser instead of one browser per agent — so N agents no
longer mean N browsers.

Two topologies, chosen by env (see :mod:`hubzoid.settings`):

  * **Direct** (no ``HUBZOID_BROWSER_CDP_URL``): the spawned ``playwright-mcp``
    launches its own headless browser; all agents share it. Memory is bounded
    by the optional RSS watchdog (``HUBZOID_BROWSER_MAX_RSS_MB``) and the host.

  * **Pooled** (``HUBZOID_BROWSER_CDP_URL`` set — a browserless container):
    ``playwright-mcp`` attaches to the pool over CDP. The pool enforces hard
    concurrency + memory limits (one action at a time, the rest queue). This is
    the recommended production shape; see ``docker/browser-compose.yml``.

Sidecar management:

  * If ``HUBZOID_BROWSER_MCP_URL`` is set, HubZoid spawns nothing and only
    points agents at that already-running endpoint (compose / production).
  * Otherwise HubZoid spawns ``npx @playwright/mcp`` itself (dev) and reaps it
    on shutdown.

The MCP wiring is done by the loader (:mod:`hubzoid.loaders.mcp`), which
auto-injects a ``playwright`` server entry pointing at :func:`endpoint_url`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("hubzoid.browser")

# The MCP server name the browser tools are grouped under. A hub that defines
# its own `playwright` entry in connectors/.mcp.json overrides the auto-inject.
SERVER_NAME = "playwright"


def _enabled_from_env() -> bool:
    return (os.environ.get("HUBZOID_BROWSER") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _port_from_env() -> int:
    raw = os.environ.get("HUBZOID_BROWSER_PORT")
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return 8931


def endpoint_url_from_env() -> str:
    """The MCP URL agents connect to, derived from env alone.

    Used by the loader, which runs before a Settings object is threaded through.
    Prefers an externally-managed endpoint (HUBZOID_BROWSER_MCP_URL); otherwise
    the local sidecar's Streamable HTTP endpoint.
    """
    external = (os.environ.get("HUBZOID_BROWSER_MCP_URL") or "").strip()
    if external:
        return external
    return f"http://localhost:{_port_from_env()}/mcp"


def mcp_config_entry_from_env() -> dict | None:
    """Return ``{SERVER_NAME: spec}`` to auto-inject, or None when disabled.

    ``spec`` is a runtime-neutral MCP server config (same shape the loader reads
    from connectors/.mcp.json). ``playwright-mcp`` serves Streamable HTTP at
    ``/mcp`` and legacy SSE at ``/sse``; we pick the transport from the URL so a
    hub can point HUBZOID_BROWSER_MCP_URL at either.
    """
    if not _enabled_from_env():
        return None
    url = endpoint_url_from_env()
    transport = "sse" if url.rstrip("/").endswith("/sse") else "streamable-http"
    # Browser actions (navigate, snapshot, wait_for) are far slower than a
    # normal tool call — a cold first navigate launches the browser. The MCP
    # client's default per-call timeout is 5s, so raise it for this server.
    return {
        SERVER_NAME: {
            "transport": transport,
            "url": url,
            "client_session_timeout_seconds": 120,
        }
    }


class BrowserManager:
    """Owns the lifecycle of the per-hub playwright-mcp sidecar.

    Inert unless the browser is enabled AND HubZoid is the one spawning the
    sidecar (i.e. HUBZOID_BROWSER_MCP_URL is not set). ``start()``/``stop()`` are
    safe to call in every deployment; they no-op where nothing needs managing.
    """

    def __init__(self, settings) -> None:
        self._s = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._watchdog: asyncio.Task | None = None
        self._log_fh = None

    @property
    def _self_managed(self) -> bool:
        # We spawn only when enabled and no external endpoint was provided.
        return bool(self._s.browser_enabled) and not self._s.browser_mcp_url

    def _spawn_cmd(self) -> list[str]:
        s = self._s
        cmd = [
            "npx", "-y", f"@playwright/mcp@{s.browser_mcp_version}",
            "--port", str(s.browser_port),
            "--headless",
            "--browser", s.browser_channel,
        ]
        if s.browser_cdp_url:
            # Pooled mode: attach to browserless. The ws form preserves the
            # token; the http form drops it on the second hop (POC-confirmed).
            # NOTE: no --isolated here. With --isolated, playwright-mcp shares a
            # single browserless browser across MCP sessions (contexts inside
            # one browser), so browserless's per-session concurrency queue never
            # engages. Without it, each session is its own browserless session,
            # so CONCURRENT/QUEUED actually gate (one action at a time, the rest
            # queue) — and browserless still isolates each session. (POC-proven.)
            cmd += ["--cdp-endpoint", s.browser_cdp_url]
        else:
            # Direct mode: no pool to isolate for us, so give each session its
            # own in-memory context over the one shared browser.
            cmd += ["--isolated"]
        return cmd

    async def start(self) -> None:
        if not self._s.browser_enabled:
            return
        if not self._self_managed:
            log.info(
                "browser: using externally-managed sidecar at %s",
                self._s.browser_mcp_url,
            )
            await self._wait_ready(endpoint_url_from_env())
            return
        if shutil.which("npx") is None:
            log.warning(
                "browser: HUBZOID_BROWSER is on but `npx` is not on PATH; "
                "cannot spawn the sidecar. Set HUBZOID_BROWSER_MCP_URL to an "
                "externally-run playwright-mcp, or install Node. Browser tools "
                "will be unavailable this run."
            )
            return

        mode = "pooled(browserless)" if self._s.browser_cdp_url else "direct(own-browser)"
        cmd = self._spawn_cmd()
        log.info("browser: starting playwright-mcp [%s] on port %s", mode, self._s.browser_port)
        self._log_fh = self._open_logfile()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=self._log_fh or asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await self._wait_ready(f"http://localhost:{self._s.browser_port}/mcp")
        except TimeoutError:
            log.warning(
                "browser: sidecar did not become ready in time; browser tools "
                "may be unavailable this run (see the sidecar log)."
            )
            return
        if self._s.browser_max_rss_mb and not self._s.browser_cdp_url:
            self._watchdog = asyncio.create_task(self._watch_rss())
        log.info("browser: playwright-mcp ready on port %s", self._s.browser_port)

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watchdog = None
        await self._kill_proc()
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None

    # -- internals ---------------------------------------------------------

    def _open_logfile(self):
        try:
            logs_dir = Path(self._s.hub_dir) / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            return open(logs_dir / "browser-mcp.log", "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None

    async def _kill_proc(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except (TimeoutError, asyncio.TimeoutError):
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def _wait_ready(self, url: str, timeout: float = 40.0) -> None:
        """Poll the MCP endpoint until it answers (any HTTP status = up)."""
        import httpx

        deadline = asyncio.get_event_loop().time() + timeout
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                # A dead spawned process will never come up — fail fast.
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"playwright-mcp exited early (code {self._proc.returncode})"
                    )
                try:
                    await client.get(url)
                    return
                except Exception as exc:  # noqa: BLE001 — connection refused etc.
                    last_err = exc
                    await asyncio.sleep(0.5)
        raise TimeoutError(f"browser sidecar not ready at {url}: {last_err}")

    async def _watch_rss(self) -> None:
        """Direct-mode safety net: restart the browser if its RSS runs away."""
        try:
            import psutil  # type: ignore
        except ImportError:
            log.warning(
                "browser: HUBZOID_BROWSER_MAX_RSS_MB is set but psutil is not "
                "installed; the memory watchdog is disabled."
            )
            return
        cap = self._s.browser_max_rss_mb
        while True:
            await asyncio.sleep(30)
            if self._proc is None or self._proc.returncode is not None:
                return
            try:
                root = psutil.Process(self._proc.pid)
                procs = [root] + root.children(recursive=True)
                rss_mb = sum(p.memory_info().rss for p in procs if p.is_running()) / (1024 * 1024)
            except Exception:  # noqa: BLE001 — process vanished mid-scan
                continue
            if rss_mb > cap:
                log.warning(
                    "browser: RSS %.0f MB exceeds cap %d MB — restarting sidecar",
                    rss_mb, cap,
                )
                await self._kill_proc()
                await self.start()
                return


def build(settings) -> BrowserManager:
    return BrowserManager(settings)
