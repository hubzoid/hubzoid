"""Portable locking and native Windows startup; no model or browser required."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hubzoid import _file_lock


def test_windows_lock_busy_and_release(tmp_path, monkeypatch):
    calls = []
    busy = [True]
    def locking(fd, mode, size):
        calls.append((mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == 1 and busy[0]:
            raise OSError(errno.EACCES, "locked")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2))
    monkeypatch.setattr(_file_lock, "_WINDOWS", True)
    with (tmp_path / "test.lock").open("a") as handle:
        with pytest.raises(BlockingIOError):
            _file_lock.lock(handle, blocking=False)
        monkeypatch.setattr(_file_lock.time, "sleep", lambda _: busy.__setitem__(0, False))
        _file_lock.lock(handle)
        _file_lock.unlock(handle)
    assert all(size == 1 and offset == 0 for _, size, offset in calls)
    assert calls[-1][0] == 2


@pytest.mark.slow
def test_file_lock_excludes_other_process_and_releases_after_exit(tmp_path):
    path = tmp_path / "test.lock"
    code = '''
import sys
from hubzoid import _file_lock
with open(sys.argv[1], 'a') as lock:
    try:
        _file_lock.lock(lock, blocking=False)
    except BlockingIOError:
        sys.exit(3)
'''
    with path.open("a") as handle:
        _file_lock.lock(handle)
        denied = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10)
        assert denied.returncode == 3
        _file_lock.unlock(handle)
    accepted = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10)
    assert accepted.returncode == 0


def test_windows_cleanup_does_not_require_sigkill(monkeypatch):
    from hubzoid import cli, _process_tree
    calls = []
    class Child:
        pid = 12345
        def poll(self): return None
        def kill(self): calls.append("kill")
        def wait(self, **kwargs): calls.append("wait")
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(cli, "signal", SimpleNamespace(SIGTERM=15))
    monkeypatch.setattr(_process_tree.subprocess, "run", lambda command, **kw: calls.append(command))
    cli._stop_groups([Child()], timeout=0)
    assert calls[0][0].endswith("taskkill.exe")
    assert calls[0][1:] == ["/PID", "12345", "/T", "/F"]
    assert calls[1:] == ["kill", "wait"]


def test_workflow_owner_is_exclusive_on_sqlite(tmp_path, monkeypatch):
    from hubzoid.workflows.ownership import Owner, OwnerBusy
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    monkeypatch.delenv("HUBZOID_DBOS_DB", raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    first, second = Owner(tmp_path, "test"), Owner(tmp_path, "test")
    first.acquire()
    try:
        with pytest.raises(OwnerBusy):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


@pytest.mark.slow
@pytest.mark.skipif(os.name != "nt", reason="native Windows startup acceptance")
def test_native_windows_hub_run_becomes_ready_and_stops(tmp_path):
    import json
    import socket
    import time
    import urllib.request

    from hubzoid.cli import _stop_groups
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: windows-test\n---\nHelp.\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUBZOID_", "AWS_"))}
    env.update(MODEL="hubzoid-test/scripted", HUBZOID_TEST_RUNTIME="1", PYTHONUTF8="1",
               BRIDGE_API_KEYS="windows-test", WEBUI_AUTH="false")
    with (tmp_path / "startup.log").open("w+") as output:
        proc = subprocess.Popen([sys.executable, "-m", "hubzoid", "run", str(hub),
                                 "--no-ui", "--bridge-port", str(port)],
                                env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        assert json.load(response)["status"] == "ok"
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                output.seek(0)
                pytest.fail(output.read())
        finally:
            _stop_groups([proc], timeout=5)
        assert proc.poll() is not None
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0



def test_windows_cleanup_timeout_does_not_skip_other_children(monkeypatch, caplog):
    from hubzoid import cli, _process_tree
    calls = []
    class Child:
        def __init__(self, pid): self.pid = pid
        def poll(self): return None
        def wait(self, *, timeout):
            calls.append((self.pid, timeout))
            if self.pid == 10:
                raise subprocess.TimeoutExpired("child", timeout)
    ticks = iter([1000, 1000, 1001])
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(_process_tree, "stop_windows_tree", lambda p: None)
    cli._stop_groups([Child(10), Child(11)], timeout=1)
    assert calls == [(10, 1), (11, 0)]
    assert "did not exit after forced shutdown" in caplog.text
