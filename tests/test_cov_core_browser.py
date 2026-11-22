"""Browser sidecar lifecycle, readiness and RSS recovery with no spawned browser."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from hubzoid import browser
from tests.test_browser_inject import _clean_browser_env  # noqa: F401


@pytest.fixture
def manager(tmp_path):
    return browser.build(SimpleNamespace(
        hub_dir=tmp_path, browser_enabled=True, browser_mcp_url="", browser_cdp_url="",
        browser_port=9876, browser_channel="chromium", browser_mcp_version="0.0.1",
        browser_max_rss_mb=0))


@pytest.mark.parametrize("value", ["bad", "0", "-1"])
def test_invalid_browser_ports_use_default(monkeypatch, value):
    monkeypatch.setenv("HUBZOID_BROWSER_PORT", value)
    assert browser.endpoint_url_from_env() == "http://localhost:8931/mcp"


@pytest.mark.asyncio
async def test_disabled_or_missing_npx_leaves_manager_inert(manager, monkeypatch, caplog):
    manager._s.browser_enabled = False
    await manager.start()
    assert manager._proc is None and manager._watchdog is None
    manager._s.browser_enabled = True
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    await manager.start()
    assert manager._proc is None and "cannot spawn the sidecar" in caplog.text


@pytest.mark.asyncio
async def test_external_endpoint_waits_without_spawning(manager, monkeypatch):
    manager._s.browser_mcp_url = "https://browser.example/mcp"
    monkeypatch.setenv("HUBZOID_BROWSER_MCP_URL", manager._s.browser_mcp_url)
    ready = AsyncMock()
    monkeypatch.setattr(manager, "_wait_ready", ready)
    await manager.start()
    ready.assert_awaited_once_with("https://browser.example/mcp")
    assert manager._proc is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
async def test_spawn_configuration_and_log_cleanup(manager, monkeypatch, pooled):
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/fake/npx")
    manager._s.browser_cdp_url = "wss://pool.example?token=secret" if pooled else ""
    proc = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(manager, "_wait_ready", AsyncMock())
    await manager.start()
    assert manager._proc is proc
    args = spawn.call_args.args
    assert args[:3] == ("npx", "-y", "@playwright/mcp@0.0.1")
    assert ("--isolated" in args) is not pooled
    if pooled:
        assert args[-2:] == ("--cdp-endpoint", "wss://pool.example?token=secret")
    logfile = manager._log_fh
    assert logfile.name.endswith("logs/browser-mcp.log")
    await manager.stop()
    assert logfile.closed and manager._log_fh is None and manager._proc is None
    proc.terminate.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_readiness_timeout_keeps_process_available_for_stop(manager, monkeypatch, caplog):
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/fake/npx")
    proc = SimpleNamespace(returncode=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(manager, "_wait_ready", AsyncMock(side_effect=TimeoutError))
    await manager.start()
    assert manager._proc is proc and manager._watchdog is None
    assert "did not become ready" in caplog.text
    await manager.stop()
    assert manager._proc is None


@pytest.mark.asyncio
async def test_stop_escalates_and_cleans_up_cancelled_watchdog(manager):
    proc = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(),
                           wait=AsyncMock(side_effect=[TimeoutError(), None]))
    manager._proc = proc
    manager._watchdog = asyncio.create_task(asyncio.sleep(100))
    manager._log_fh = Mock()
    manager._log_fh.close.side_effect = OSError("closed elsewhere")
    await manager.stop()
    proc.kill.assert_called_once()
    assert proc.wait.await_count == 2
    assert manager._proc is None and manager._watchdog is None and manager._log_fh is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["terminate", "kill"])
async def test_process_disappearing_during_shutdown_is_harmless(manager, stage):
    proc = SimpleNamespace(returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock())
    getattr(proc, stage).side_effect = ProcessLookupError()
    if stage == "kill":
        proc.wait.side_effect = TimeoutError()
    manager._proc = proc
    await manager._kill_proc()
    assert manager._proc is None
    getattr(proc, stage).assert_called_once()


def test_unwritable_log_folder_disables_file_logging(manager):
    manager._s.hub_dir = manager._s.hub_dir / "not-a-directory"
    manager._s.hub_dir.write_text("a file cannot hold a logs directory")
    assert manager._open_logfile() is None


class _Client:
    def __init__(self, get):
        self.get = get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_readiness_retries_connection_failure_and_accepts_http_error(manager, monkeypatch):
    get = AsyncMock(side_effect=[httpx.ConnectError("cold"), httpx.Response(503)])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(get))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await manager._wait_ready("http://fake/mcp", timeout=1)
    assert get.await_count == 2
    assert get.call_args.args == ("http://fake/mcp",)


@pytest.mark.asyncio
async def test_readiness_reports_dead_process_and_deadline(manager, monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(get))
    manager._proc = SimpleNamespace(returncode=17)
    with pytest.raises(RuntimeError, match="exited early.*17"):
        await manager._wait_ready("http://fake/mcp", timeout=1)
    manager._proc = None
    with pytest.raises(TimeoutError, match="not ready at http://fake/mcp"):
        await manager._wait_ready("http://fake/mcp", timeout=0)
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_missing_psutil_and_finished_process(manager, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "psutil", None)
    await manager._watch_rss()
    assert "memory watchdog is disabled" in caplog.text
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    manager._proc = SimpleNamespace(returncode=0)
    await manager._watch_rss()
    assert manager._proc.returncode == 0


@pytest.mark.asyncio
async def test_watchdog_counts_children_and_restarts_over_limit(manager, monkeypatch, caplog):
    manager._s.browser_max_rss_mb = 5
    manager._proc = SimpleNamespace(returncode=None, pid=12345)
    child = SimpleNamespace(is_running=lambda: True, memory_info=lambda: SimpleNamespace(rss=4 * 1024**2))
    root = SimpleNamespace(is_running=lambda: True, memory_info=lambda: SimpleNamespace(rss=2 * 1024**2),
                           children=lambda **kw: [child])
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda pid: root))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    kill, start = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_kill_proc", kill)
    monkeypatch.setattr(manager, "start", start)
    await manager._watch_rss()
    kill.assert_awaited_once()
    start.assert_awaited_once()
    assert "RSS 6 MB exceeds cap 5 MB" in caplog.text


@pytest.mark.asyncio
async def test_watchdog_retries_when_process_vanishes_during_scan(manager, monkeypatch):
    manager._proc = SimpleNamespace(returncode=None, pid=12345)
    process = Mock(side_effect=ProcessLookupError())
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=process))

    async def sleep(_):
        if process.called:
            manager._proc = None

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await manager._watch_rss()
    process.assert_called_once_with(12345)
    assert manager._proc is None
