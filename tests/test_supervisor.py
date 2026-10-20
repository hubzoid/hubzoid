"""hubzoid.supervisor: units restart on their own with backoff, a unit that
never comes up is marked failed, a restart drains first, the routing table
carries each hub's state, and control commands go over the socket.

Processes are stand-ins (no subprocess is started)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hubzoid import supervisor as sup_lib


class Proc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.code = None
        self.pid = None
        self.stdout = iter(())
        self.signals = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0 if self.code is None else self.code
        return self.code

    def terminate(self):
        self.signals.append("TERM")
        self.code = -15

    def kill(self):
        self.signals.append("KILL")
        self.code = -9


@pytest.fixture
def started(monkeypatch):
    procs = []

    def popen(cmd, env=None, **kw):
        p = Proc(cmd)
        procs.append(p)
        return p

    monkeypatch.setattr(sup_lib.subprocess, "Popen", popen)
    monkeypatch.setattr(sup_lib.time, "sleep", lambda s: None)
    return procs


def _sup(tmp_path, units, *, probe=lambda u: True, **kw):
    return sup_lib.Supervisor(units, state_dir=tmp_path / "state", probe=probe, say=lambda t: None,
                              table={"default": "http://127.0.0.1:1", "routes": []}, **{"ready_wait": 1, **kw})


def _unit(name, **kw):
    return sup_lib.Unit(name=name, slug=name, specs=[sup_lib.Spec(["bridge", name], {})], **kw)


def test_a_part_started_unit_keeps_and_stops_what_already_runs(tmp_path, started, monkeypatch):
    """A stop or a failed spawn part-way through a unit (bridge, then its
    channel) must not leave the processes already started unrecorded."""
    unit = sup_lib.Unit(name="a", slug="a", specs=[sup_lib.Spec(["bridge", "a"], {}),
                                                   sup_lib.Spec(["channel", "a"], {}, role="channel")])
    sup = _sup(tmp_path, [unit])
    real = sup_lib.subprocess.Popen

    def second_fails(cmd, env=None, **kw):
        if cmd[0] == "channel":
            raise OSError("no such program")
        return real(cmd, env=env, **kw)

    monkeypatch.setattr(sup_lib.subprocess, "Popen", second_fails)
    assert sup.launch(unit) is False
    assert unit.procs == [] and started[0].signals  # the bridge was stopped, not leaked

    def interrupted(cmd, env=None, **kw):
        if cmd[0] == "channel":
            raise SystemExit(0)  # SIGTERM arriving between the two spawns
        return real(cmd, env=env, **kw)

    monkeypatch.setattr(sup_lib.subprocess, "Popen", interrupted)
    with pytest.raises(SystemExit):
        sup.launch(unit)
    assert unit.procs == [started[1]]  # recorded, so the caller's cleanup stops it


def test_a_hub_that_exits_is_started_again_alone(tmp_path, started):
    a, b = _unit("a"), _unit("b")
    sup = _sup(tmp_path, [a, b])
    assert sup.start_all([a, b]) == {"a": True, "b": True}
    a.procs[0].code = 1                       # a crashes
    sup.check_once()
    assert a.state == "restarting" and b.state == "ready" and b.procs[0].code is None
    a.retry_at = 0
    sup._retry(a)                             # the backoff is over
    assert a.state == "ready" and a.restarts == 1 and len(started) == 3
    table = json.loads((tmp_path / "state" / "routes.json").read_text())
    assert table["states"] == {"a": "ready", "b": "ready"}


def test_backoff_doubles_and_five_failed_starts_mark_the_hub_failed(tmp_path, started):
    bad = _unit("bad")
    sup = _sup(tmp_path, [bad], probe=lambda u: False)
    delays = []
    for _ in range(5):
        sup._start(bad)
        delays.append(bad.delay)
    assert delays[:4] == [1, 2, 4, 8] and bad.state == "failed"
    assert "did not answer" in sup.status()["units"][0]["error"]
    # It stays stopped: the loop never retries a failed hub.
    bad.retry_at = 0
    sup.check_once()
    assert bad.state == "failed"



def test_a_bridge_answering_as_another_hub_fails_its_start_at_once(tmp_path, started):
    """A folder that now names another agent: no waiting out the ready timeout."""
    unit = _unit("a")
    sup = _sup(tmp_path, [unit], probe=lambda u: "answers as 'b', not 'a'", ready_wait=3600)
    assert sup._start(unit) is False
    assert unit.state == "restarting" and "answers as 'b'" in unit.last_exit

def test_a_restart_drains_first_and_stops_channels_before_the_bridge(tmp_path, started, monkeypatch):
    order = []
    unit = sup_lib.Unit(name="sales", slug="sales", bridge_url="http://127.0.0.1:9", key="k",
                        specs=[sup_lib.Spec(["bridge"], {}), sup_lib.Spec(["slack"], {}, role="channel")])
    sup = _sup(tmp_path, [unit])
    sup.start_all([unit])
    bridge, slack = unit.procs

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"busy": False}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, **k: order.append(("drain", url)) or R())
    monkeypatch.setattr(httpx, "get", lambda url, **k: order.append(("check", url)) or R())
    monkeypatch.setattr(sup_lib, "_signal_group", lambda p, sig: order.append(("stop", p.cmd[0])) or p.terminate())
    out = sup.handle({"op": "restart", "hubs": ["sales"]})
    assert out["ok"] and unit.state == "ready"
    drained = order.index(("drain", "http://127.0.0.1:9/internal/drain"))
    assert order.index(("stop", "slack")) < drained            # 1.x channels finish first
    assert order.index(("stop", "bridge")) > drained           # the bridge stops last


def test_reload_checks_the_folder_and_changes_nothing_when_it_is_broken(tmp_path, started):
    unit = _unit("ops")
    sup = _sup(tmp_path, [unit], validate=lambda u: ["tools_local/x.py: SyntaxError"])
    sup.start_all([unit])
    first = unit.procs[0]
    out = sup.handle({"op": "reload", "hubs": ["ops"]})
    assert out["ok"] is False and out["results"][0]["problems"] == ["tools_local/x.py: SyntaxError"]
    assert unit.procs[0] is first and first.code is None


def test_unknown_hubs_and_status(tmp_path, started):
    unit = _unit("ops")
    sup = _sup(tmp_path, [unit])
    sup.start_all([unit])
    assert "No hub named nope" in sup.handle({"op": "restart", "hubs": ["nope"]})["error"]
    rows = sup.handle({"op": "status"})["units"]
    assert rows[0]["hub"] == "ops" and rows[0]["state"] == "ready" and rows[0]["reload_needed"] == []


def test_status_says_when_a_reload_is_needed(tmp_path, started):
    hub = tmp_path / "ops"
    hub.mkdir()
    (hub / ".env").write_text("A=1\n")
    unit = _unit("ops", hub_dir=hub)
    sup = _sup(tmp_path, [unit])
    sup.start_all([unit])
    (hub / ".env").write_text("A=22\n")
    assert sup.status()["units"][0]["reload_needed"]


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="Unix sockets")
def test_control_commands_go_over_the_socket_and_one_supervisor_holds_the_lock(tmp_path, started):
    unit = _unit("ops")
    sup = _sup(tmp_path, [unit])
    sup.lock()
    other = _sup(tmp_path, [_unit("ops")])
    with pytest.raises(RuntimeError, match="already running"):
        other.lock()
    sup.start_all([unit])
    sup._serve_control()
    try:
        out = sup_lib.request(tmp_path / "state", {"op": "status"}, timeout=10)
        assert out["ok"] and out["units"][0]["hub"] == "ops"
    finally:
        sup.shutdown()
    with pytest.raises(ConnectionError, match="No running deployment"):
        sup_lib.request(tmp_path / "state", {"op": "status"})
