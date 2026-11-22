"""Supervisor external health, control failures and process cleanup with stand-ins."""
from __future__ import annotations

import hashlib
import json
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from hubzoid import supervisor as sv
from tests.test_supervisor import Proc, _sup, _unit, started  # noqa: F401


def test_external_service_refuses_local_control_and_recovers_health(tmp_path):
    unit = _unit("external", kind="external", bridge_url="https://service.example")
    sup = _sup(tmp_path, [unit], probe=lambda u: "wrong hub")
    unit.state = "ready"
    for _ in range(sv.EXTERNAL_MISSES):
        sup._check_external(unit)
    assert unit.state == "restarting" and unit.error == "wrong hub"
    assert unit.delay == sv.EXTERNAL_PROBE_SECONDS
    for operation in (sup.start, sup.stop, sup.restart):
        result = operation(unit)
        assert result["ok"] is False and "runs as its own service" in result["error"]
    sup.probe = lambda u: True
    sup._retry(unit)
    assert unit.state == "ready" and unit.misses == 0 and unit.restarts == 0
    assert unit.next_probe > unit.ready_since


def test_locked_unit_ignores_monitor_and_retry(tmp_path):
    unit = _unit("ops", kind="external")
    sup = _sup(tmp_path, [unit], probe=lambda u: pytest.fail("locked unit must not be probed"))
    unit.state = "ready"
    with unit.lock:
        sup.check_once()
        sup._check_external(unit)
        sup._retry(unit)
    assert unit.state == "ready" and unit.misses == 0


def test_monitor_resets_backoff_after_stability_and_schedules_external_check(tmp_path, monkeypatch):
    unit = _unit("ops")
    unit.state, unit.delay = "ready", 8
    unit.procs = [Proc(["bridge"])]
    unit.ready_since = 1
    unit.failures = [1]
    external = _unit("external", kind="external")
    external.state, external.next_probe = "ready", 0
    sup = _sup(tmp_path, [unit, external])
    targets = []
    monkeypatch.setattr(sv.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: targets.append(kw)))
    sup.check_once()
    assert unit.delay == 0 and unit.failures == []
    assert targets[0]["target"] == sup._check_external and targets[0]["args"] == (external,)
    assert external.next_probe > 0


def test_stop_and_start_commands_change_unit_state(tmp_path, started):
    unit = _unit("ops")
    sup = _sup(tmp_path, [unit])
    assert sup.handle({"op": "start", "hubs": ["ops"]})["ok"] is True
    assert unit.state == "ready"
    first = unit.procs[0]
    assert sup.handle({"op": "start", "hubs": ["ops"]})["ok"] is True
    assert len(started) == 1
    assert sup.handle({"op": "stop", "hubs": ["ops"], "grace": 0})["results"][0]["state"] == "stopped"
    assert first.signals and unit.procs == []
    assert sup.handle({"op": "start", "hubs": []}) == {"ok": False, "error": "Name at least one hub."}


def test_log_commands_tail_and_missing_file(tmp_path):
    unit = _unit("ops")
    sup = _sup(tmp_path, [unit])
    assert sup.handle({"op": "logs", "hub": "missing"}) == {"ok": False, "error": "No hub named missing here."}
    assert sup.handle({"op": "logs", "hub": "ops"})["lines"] == []
    sup.log_dir.mkdir(parents=True)
    (sup.log_dir / "ops.log").write_text("one\ntwo\nthree\n")
    assert sup.handle({"op": "logs", "hub": "ops", "lines": 2})["lines"] == ["two", "three"]


@pytest.mark.parametrize("body", ["{", '{}'])
def test_control_pointer_missing_socket_and_remote_host_are_named(tmp_path, monkeypatch, body):
    pointer = sv.control_file(tmp_path)
    pointer.write_text(body)
    socket = Mock()
    socket.__enter__ = Mock(return_value=socket)
    socket.__exit__ = Mock(return_value=False)
    socket.connect.side_effect = OSError("gone")
    monkeypatch.setattr(sv.socket, "socket", lambda *a: socket)
    with pytest.raises(ConnectionError, match="No running deployment|no answer on its control socket"):
        sv.request(tmp_path, {"op": "status"})
    pointer.write_text(json.dumps({"host": "definitely-another-host"}))
    with pytest.raises(ConnectionError, match="not here"):
        sv.request(tmp_path, {"op": "status"})


def test_control_protocol_reads_chunks_and_empty_response(tmp_path, monkeypatch):
    sv.control_file(tmp_path).write_text('{"socket":"fake"}')
    sent = []
    replies = iter([b'{"ok":', b'true}\n'])

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, timeout): self.timeout = timeout
        def connect(self, path): self.path = path
        def sendall(self, value): sent.append(value)
        def recv(self, n): return next(replies)

    monkeypatch.setattr(sv.socket, "socket", lambda *a: Socket())
    assert sv.request(tmp_path, {"op": "status"}) == {"ok": True}
    assert json.loads(sent[0]) == {"op": "status"}
    replies = iter([b""])
    assert sv.request(tmp_path, {}) == {}


def test_malformed_control_command_returns_error_without_stopping_server(tmp_path, monkeypatch):
    sup = _sup(tmp_path, [])
    socket_dir = tmp_path / "socket-dir"
    socket_dir.mkdir()
    path = socket_dir / f"{hashlib.sha256(str(sup.state_dir).encode()).hexdigest()[:16]}.sock"
    path.write_text("stale")
    monkeypatch.setattr(sv, "_socket_dir", lambda: socket_dir)
    monkeypatch.setattr(sv.os, "chmod", lambda *a: None)
    replies = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def recv(self, n): return b"not-json\n"
        def sendall(self, data): replies.append(json.loads(data))

    server = Mock()
    server.accept.side_effect = [(Connection(), None), OSError("closed")]
    monkeypatch.setattr(sv.socket, "socket", lambda *a: server)

    class Thread:
        def __init__(self, target, args=(), **kw): self.target, self.args = target, args
        def start(self): self.target(*self.args)

    monkeypatch.setattr(sv.threading, "Thread", Thread)
    sup._serve_control()
    pointer = json.loads(sv.control_file(sup.state_dir).read_text())
    assert pointer["generation"] == sup.generation and pointer["socket"] == str(path)
    assert replies[0]["ok"] is False and "Expecting value" in replies[0]["error"]
    assert server.accept.call_count == 2 and not sup._stop.is_set()


def test_parent_watch_ignores_permission_denial_and_stops_after_parent_disappears(monkeypatch):
    kills = []
    probes = iter([PermissionError(), ProcessLookupError()])

    def kill(pid, sig):
        kills.append((pid, sig))
        if pid == 1234:
            raise next(probes)

    monkeypatch.setattr(sv, "os", SimpleNamespace(name="posix", environ={sv.ENV_PID: "1234"},
                                                kill=kill, getpid=lambda: 5678))
    monkeypatch.setattr(sv.time, "sleep", lambda value: None)
    monkeypatch.setattr(sv.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))
    sv.watch_parent(interval=0)
    assert kills == [(1234, 0), (1234, 0), (5678, signal.SIGTERM)]


def test_process_stop_escalates_unresponsive_child_and_ignores_signal_races():
    proc = Proc(["bridge"])
    proc.wait = Mock(side_effect=subprocess.TimeoutExpired("bridge", 0))
    sv.stop_procs([proc, None], timeout=0)
    assert proc.signals == ["TERM", "KILL", "KILL"]
    proc.terminate = Mock(side_effect=ProcessLookupError())
    sv._signal_group(proc, signal.SIGTERM)
    proc.terminate.assert_called_once()


@pytest.mark.parametrize(("response", "expected"), [
    (httpx.Response(503), False), (httpx.Response(200, text="invalid"), False),
    (httpx.Response(200, json={"hub": "other"}), "port answers as 'other', not 'ops'"),
    (httpx.Response(200, json={"hub": "ops", "model": "old"}), "the agent now loads as 'old'"),
    (httpx.Response(200, json={"hub": "ops", "model": "wanted"}), True),
])
def test_health_probe_validates_identity_and_json(monkeypatch, response, expected):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: response)
    unit = _unit("ops", health_url="http://fake/healthz", identity={"expected_hub": "ops", "expected_model": "wanted"})
    result = sv._probe(unit)
    assert result is expected if isinstance(expected, bool) else result.startswith(expected)


def test_health_probe_transport_failure_and_absent_health_url(monkeypatch):
    monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ConnectError("offline")))
    assert sv._probe(_unit("ops", health_url="http://fake")) is False
    assert sv._probe(_unit("ops")) is True
