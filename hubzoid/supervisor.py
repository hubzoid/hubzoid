"""`hubzoid run`'s supervisor: every hub its own process group, restarted alone.

A deployment is a set of **units**: the edge, and one unit per hub (its bridge,
then its 1.x channel processes). The supervisor

* restarts a unit whose process exits, waiting 1 s, then doubling to 60 s, and
  marks it **failed** after 5 starts in 10 minutes that never became ready;
  it never ends because a hub ended;
* writes the edge's routing table file with each hub's state (``hubzoid.edge``
  answers 503 "restarting" for a hub that is down, and refuses new webhooks to
  one that drains);
* answers control commands (``hubzoid status|restart|stop|start|reload|logs``)
  over a Unix socket only its own user can open, one lifecycle operation per
  hub at a time;
* drains before a restart: the edge stops sending the hub webhooks, the hub's
  1.x channel processes stop (finishing what they acknowledged), the bridge
  puts up its drain barrier (``hubzoid.drain``), and running work gets the
  grace period before the process stops;
* keeps each unit's output in ``<state>/logs/<unit>.log`` as well as printing
  it, so ``hubzoid logs <hub>`` works under systemd too.

Children watch for their supervisor to disappear (``watch_parent``) so a
killed supervisor never leaves processes holding ports.

Pure process management: no runtime SDK, no hub loading. ``hubzoid.cli``
builds the units (the same planning `run` and `gateway` always did).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("hubzoid.supervisor")

ENV_PID = "HUBZOID_SUPERVISOR_PID"
GRACE_SECONDS = 120.0
BACKOFF_MAX = 60.0
FAIL_LIMIT = 5
FAIL_WINDOW = 600.0
STABLE_AFTER = 600.0
LOG_BYTES = 10 * 1024 * 1024
LOG_KEEP = 3

# What a hub reads only when it starts: a change to any of these needs a
# reload (a checked restart). Knowledge and hitl.yaml are read when used.
_STAMPED = (".env", "AGENTS.md", "agents", "skills", "tools_local", "restricted", "channels",
            "connectors", "cards", "mcp-apps.yaml", "settings.yaml", "hub.settings.yaml",
            "requirements.txt", "workflows", "schedule")


@dataclass
class Spec:
    """How to start one process of a unit."""
    cmd: list[str]
    env: dict
    role: str = "bridge"  # bridge | channel | edge


@dataclass
class Unit:
    name: str                    # the hub key (folder name) or "edge"
    specs: list[Spec]
    health_url: str = ""
    identity: dict = field(default_factory=dict)  # expected_hub / expected_model
    bridge_url: str = ""         # for drain calls ("" for the edge)
    key: str = ""                # a bridge key for drain calls
    slug: str = ""
    hub_dir: Path | None = None
    kind: str = "hub"            # hub | edge
    procs: list = field(default_factory=list)
    state: str = "stopped"       # starting ready draining restarting failed stopped
    generation: int = 0
    restarts: int = 0
    started_at: float = 0.0
    ready_since: float = 0.0
    retry_at: float = 0.0
    delay: float = 0.0
    failures: list = field(default_factory=list)
    last_exit: str = ""
    error: str = ""
    stamp: str = ""
    busy: bool = False           # a lifecycle operation holds it
    lock: threading.Lock = field(default_factory=threading.Lock)

    def bridge_proc(self):
        return self.procs[0] if self.procs else None


# -- processes ---------------------------------------------------------------------------

def _rotate(path: Path) -> None:
    for i in range(LOG_KEEP - 1, 0, -1):
        older = path.with_suffix(f".log.{i}")
        if older.exists():
            older.replace(path.with_suffix(f".log.{i + 1}"))
    path.replace(path.with_suffix(".log.1"))


class _Log:
    """A unit's log file, shared by its processes, rotated by size."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str) -> None:
        if self.path is None:
            return
        with self.lock:
            try:
                if self.path.exists() and self.path.stat().st_size > LOG_BYTES:
                    _rotate(self.path)
                with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(text)
            except OSError:
                pass


_logs: dict[str, _Log] = {}


def log_for(name: str, log_dir: Path | None) -> _Log:
    key = f"{log_dir}/{name}"
    if key not in _logs:
        _logs[key] = _Log(Path(log_dir) / f"{name}.log" if log_dir else None)
    return _logs[key]


def _pump(proc, name: str, sink: _Log) -> None:
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return
    try:
        for raw in stream:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            sink.write(line)
            try:
                sys.stdout.write(f"[{name}] {line}")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
    except (OSError, ValueError, TypeError):
        pass


def spawn(spec: Spec, *, name: str, log_dir: Path | None = None):
    """Start one process in its own session (process group), its output
    copied to the unit's log file and to this process's output."""
    env = dict(spec.env)
    env[ENV_PID] = str(os.getpid())
    proc = subprocess.Popen(spec.cmd, env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    threading.Thread(target=_pump, args=(proc, name, log_for(name, log_dir)),
                     name=f"log-{name}", daemon=True).start()
    return proc


def _signal_group(proc, sig) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if os.name != "nt" and type(pid) is int and pid > 1:
            os.killpg(pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop_procs(procs, *, timeout: float = 15.0) -> None:
    """Stop processes and their groups, last started first, each given
    ``timeout`` to finish before it is killed."""
    for proc in reversed([p for p in procs if p is not None]):
        if proc.poll() is None:
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        _signal_group(proc, signal.SIGKILL)  # anything it left in its group


def watch_parent(*, interval: float = 2.0) -> None:
    """In a supervised child: stop this process when the supervisor that
    started it is gone (it was killed, not stopped), so nothing keeps holding a
    port. No-op outside a supervisor."""
    raw = (os.environ.get(ENV_PID) or "").strip()
    if not raw.isdigit() or os.name == "nt":
        return
    parent = int(raw)

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                os.kill(parent, 0)
            except ProcessLookupError:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            except PermissionError:
                pass

    threading.Thread(target=loop, name="watch-supervisor", daemon=True).start()


# -- folder stamps -------------------------------------------------------------------------

def stamp(hub_dir: Path | None) -> str:
    """A digest of what a hub reads only at start (names, sizes and times)."""
    if hub_dir is None:
        return ""
    h = hashlib.sha256()
    for name in _STAMPED:
        base = Path(hub_dir) / name
        paths = [base] if base.is_file() else (sorted(p for p in base.rglob("*") if p.is_file()
                                                       and "__pycache__" not in p.parts)
                                               if base.is_dir() else [])
        for p in paths:
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.relative_to(hub_dir)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def changed(hub_dir: Path | None, since: str) -> list[str]:
    """Which stamped parts of a hub differ from ``since`` (names only)."""
    if hub_dir is None or not since or stamp(hub_dir) == since:
        return []
    return ["the hub folder changed since it started (.env, instructions, tools, channels or "
            "connectors)"]


# -- control socket ------------------------------------------------------------------------

def _socket_dir() -> Path:
    base = Path(os.environ.get("TMPDIR") or "/tmp") / f"hubzoid-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    return base


def control_file(state_dir: Path) -> Path:
    return Path(state_dir) / "control.json"


def request(state_dir: Path, payload: dict, *, timeout: float = 600.0) -> dict:
    """Send one command to the supervisor of the deployment whose state is in
    ``state_dir``. Raises ConnectionError with a plain message when none runs."""
    pointer = control_file(state_dir)
    try:
        info = json.loads(pointer.read_text())
    except (OSError, ValueError):
        raise ConnectionError(f"No running deployment found in {state_dir}. Is `hubzoid run` running?")
    path = info.get("socket") or ""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(path)
        except OSError:
            raise ConnectionError(f"The deployment in {state_dir} is not running (no answer on its "
                                  "control socket). Start it with `hubzoid run`.")
        s.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode() or "{}")


# -- the supervisor ------------------------------------------------------------------------

class Supervisor:
    def __init__(self, units: list[Unit], *, state_dir: Path, table: dict | None = None,
                 grace: float = GRACE_SECONDS, probe: Callable[[Unit], bool] | None = None,
                 validate: Callable[[Unit], list[str]] | None = None,
                 say: Callable[[str], None] = print, start_concurrency: int = 3,
                 ready_wait: float = 180.0):
        self.units = {u.name: u for u in units}
        self.state_dir = Path(state_dir)
        self.log_dir = self.state_dir / "logs"
        self.table = table or {}
        self.grace = grace
        self.probe = probe or _probe
        self.validate = validate or (lambda unit: [])
        self.say = say
        self.start_concurrency = max(1, start_concurrency)
        self.ready_wait = ready_wait
        self._stop = threading.Event()
        self._table_lock = threading.Lock()
        self._server = None
        self._lock_fh = None
        self.generation = os.urandom(6).hex()
        # More folders to leave the control pointer in (the deployment folder,
        # when its state is kept elsewhere with --data-dir).
        self.pointers: list[Path] = []

    # -- lifecycle of one unit ---------------------------------------------------------
    def _start(self, unit: Unit, *, wait: float | None = None) -> bool:
        return self.launch(unit) and self.await_ready(unit, wait=wait)

    def launch(self, unit: Unit) -> bool:
        """Start a unit's processes (bridge first) without waiting."""
        unit.generation += 1
        unit.state, unit.error = "starting", ""
        unit.started_at = time.time()
        unit.stamp = stamp(unit.hub_dir)
        self._write_table()
        try:
            unit.procs = [spawn(spec, name=unit.name, log_dir=self.log_dir) for spec in unit.specs]
        except OSError as exc:
            unit.procs = []
            return self._failed_start(unit, f"could not start: {exc}")
        return True

    def await_ready(self, unit: Unit, *, wait: float | None = None) -> bool:
        """Wait for a launched unit's health check: ready, or a failed start
        that is retried with backoff."""
        wait = self.ready_wait if wait is None else wait
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not self._stop.is_set():
            if any(p.poll() is not None for p in unit.procs[:1]):
                return self._failed_start(unit, f"exited during start (status {unit.procs[0].poll()})")
            if self.probe(unit):
                unit.state, unit.ready_since = "ready", time.time()
                self._write_table()
                self.say(f"→ {unit.name}  ready")
                return True
            time.sleep(0.5)
        return self._failed_start(unit, f"did not answer within {wait:.0f} s")

    def _failed_start(self, unit: Unit, why: str) -> bool:
        stop_procs(unit.procs, timeout=5)
        unit.last_exit = why
        self._schedule_retry(unit, why)
        return False

    def _schedule_retry(self, unit: Unit, why: str) -> None:
        now = time.time()
        unit.failures = [t for t in unit.failures if now - t < FAIL_WINDOW] + [now]
        if len(unit.failures) >= FAIL_LIMIT:
            unit.state, unit.error = "failed", why
            self.say(f"→ {unit.name}  failed ({why}); it stays stopped until `hubzoid start {unit.name}`")
        else:
            unit.delay = min(BACKOFF_MAX, max(1.0, unit.delay * 2 if unit.delay else 1.0))
            unit.retry_at = now + unit.delay
            unit.state, unit.error = "restarting", why
            self.say(f"→ {unit.name}  {why}; starting again in {unit.delay:.0f} s")
        self._write_table()

    def _drain(self, unit: Unit, grace: float) -> None:
        """Webhooks off at the edge, 1.x channels stopped, then the bridge's
        barrier up and running work given ``grace`` seconds."""
        unit.state = "draining"
        self._write_table()
        stop_procs(unit.procs[1:], timeout=min(grace, 60.0))
        if not unit.bridge_url or not unit.procs or unit.procs[0].poll() is not None:
            return
        import httpx

        headers = {"Authorization": f"Bearer {unit.key}"} if unit.key else {}
        try:
            httpx.post(unit.bridge_url + "/internal/drain", headers=headers, timeout=5)
        except httpx.HTTPError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                r = httpx.get(unit.bridge_url + "/internal/drain", headers=headers, timeout=5)
                if r.status_code != 200 or not r.json().get("busy"):
                    return
            except (httpx.HTTPError, ValueError):
                return
            time.sleep(1.0)
        self.say(f"→ {unit.name}  grace period over; stopping what still runs")

    def _stop_unit(self, unit: Unit, *, grace: float) -> None:
        if unit.procs and any(p.poll() is None for p in unit.procs):
            self._drain(unit, grace)
        stop_procs(unit.procs[:1])
        unit.procs = []

    def restart(self, unit: Unit, *, grace: float | None = None, check: bool = False) -> dict:
        with unit.lock:
            unit.busy = True
            try:
                if check:
                    problems = self.validate(unit)
                    if problems:
                        return {"ok": False, "hub": unit.name, "problems": problems}
                self._stop_unit(unit, grace=self.grace if grace is None else grace)
                unit.restarts += 1
                unit.failures, unit.delay = [], 0.0
                ok = self._start(unit)
                return {"ok": ok, "hub": unit.name, "state": unit.state, "error": unit.error}
            finally:
                unit.busy = False

    def stop(self, unit: Unit, *, grace: float | None = None) -> dict:
        with unit.lock:
            unit.busy = True
            try:
                self._stop_unit(unit, grace=self.grace if grace is None else grace)
                unit.state = "stopped"
                self._write_table()
                return {"ok": True, "hub": unit.name, "state": unit.state}
            finally:
                unit.busy = False

    def start(self, unit: Unit) -> dict:
        with unit.lock:
            unit.busy = True
            try:
                if unit.procs and all(p.poll() is None for p in unit.procs):
                    return {"ok": True, "hub": unit.name, "state": unit.state}
                unit.failures, unit.delay = [], 0.0
                ok = self._start(unit)
                return {"ok": ok, "hub": unit.name, "state": unit.state, "error": unit.error}
            finally:
                unit.busy = False

    # -- monitoring --------------------------------------------------------------------
    def check_once(self) -> None:
        """Notice exits and due retries. Called by the loop; tests call it."""
        now = time.time()
        for unit in list(self.units.values()):
            if unit.busy or unit.lock.locked():
                continue
            if unit.state == "restarting" and now >= unit.retry_at:
                threading.Thread(target=self._retry, args=(unit,), daemon=True).start()
                continue
            if unit.state != "ready":
                continue
            dead = [p for p in unit.procs if p.poll() is not None]
            if not dead:
                if unit.delay and now - unit.ready_since > STABLE_AFTER:
                    unit.delay, unit.failures = 0.0, []
                continue
            code = dead[0].poll()
            stop_procs(unit.procs, timeout=5)
            unit.procs = []
            # A process that ran for a while counts as a crash, not a failed start.
            if now - unit.ready_since > 60:
                unit.failures = []
            self._schedule_retry(unit, f"exited (status {code})")

    def _retry(self, unit: Unit) -> None:
        if not unit.lock.acquire(blocking=False):
            return
        try:
            unit.busy = True
            if unit.state == "restarting":
                unit.restarts += 1
                self._start(unit)
        finally:
            unit.busy = False
            unit.lock.release()

    def status(self) -> dict:
        out = []
        for unit in self.units.values():
            pid = getattr(unit.bridge_proc(), "pid", None)
            row = {"hub": unit.name, "kind": unit.kind, "slug": unit.slug, "state": unit.state,
                   "pid": pid if isinstance(pid, int) else None,
                   "since": unit.ready_since or unit.started_at, "restarts": unit.restarts,
                   "last_exit": unit.last_exit, "error": unit.error,
                   "retry_in": max(0.0, round(unit.retry_at - time.time(), 1)) if unit.state == "restarting" else None,
                   "reload_needed": changed(unit.hub_dir, unit.stamp) if unit.state == "ready" else []}
            out.append(row)
        return {"generation": self.generation, "pid": os.getpid(), "units": out}

    # -- the routing table -------------------------------------------------------------
    def _write_table(self) -> None:
        if not self.table:
            return
        data = dict(self.table)
        data["states"] = {u.slug: u.state for u in self.units.values() if u.slug}
        path = self.state_dir / "routes.json"
        with self._table_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(path)

    # -- control commands --------------------------------------------------------------
    def _named(self, names) -> tuple[list[Unit], list[str]]:
        found, missing = [], []
        for n in names or []:
            n = str(n).strip().lower()
            unit = self.units.get(n) or next((u for u in self.units.values() if u.slug == n), None)
            (found.append(unit) if unit is not None else missing.append(n))
        return found, missing

    def handle(self, cmd: dict) -> dict:
        op = cmd.get("op")
        if op == "status":
            return {"ok": True, **self.status()}
        if op == "logs":
            units, missing = self._named([cmd.get("hub")])
            if missing:
                return {"ok": False, "error": f"No hub named {missing[0]} here."}
            path = self.log_dir / f"{units[0].name}.log"
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            return {"ok": True, "path": str(path), "lines": lines[-int(cmd.get("lines") or 200):]}
        if op in ("restart", "reload", "stop", "start"):
            units, missing = self._named(cmd.get("hubs"))
            if missing:
                return {"ok": False, "error": "No hub named " + ", ".join(missing) + " here. "
                                              "`hubzoid status` lists them."}
            if not units:
                return {"ok": False, "error": "Name at least one hub."}
            grace = cmd.get("grace")
            results = []
            for unit in units:  # one after the other
                if op == "stop":
                    results.append(self.stop(unit, grace=grace))
                elif op == "start":
                    results.append(self.start(unit))
                else:
                    results.append(self.restart(unit, grace=grace, check=(op == "reload")))
            return {"ok": all(r.get("ok") for r in results), "results": results}
        return {"ok": False, "error": f"unknown command {op!r}"}

    def _serve_control(self) -> None:
        if os.name == "nt":
            return
        path = _socket_dir() / f"{hashlib.sha256(str(self.state_dir).encode()).hexdigest()[:16]}.sock"
        if path.exists():
            path.unlink()  # stale: this supervisor holds the deployment lock
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(8)
        self._server = srv
        info = json.dumps({"socket": str(path), "pid": os.getpid(), "generation": self.generation})
        for folder in [self.state_dir, *self.pointers]:
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
                control_file(folder).write_text(info)
            except OSError:
                pass

        def client(conn: socket.socket) -> None:
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    reply = self.handle(json.loads(data.decode() or "{}"))
                except Exception as exc:  # noqa: BLE001 - a bad command never stops the supervisor
                    log.exception("supervisor: command failed")
                    reply = {"ok": False, "error": str(exc)}
                try:
                    conn.sendall(json.dumps(reply, default=str).encode() + b"\n")
                except OSError:
                    pass

        def accept() -> None:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=client, args=(conn,), daemon=True).start()

        threading.Thread(target=accept, name="control", daemon=True).start()

    def lock(self) -> None:
        """Hold the deployment's lock for this process's lifetime. Raises
        RuntimeError when another supervisor holds it."""
        if os.name == "nt":
            return
        import fcntl

        self.state_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.state_dir / "supervisor.lock", "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise RuntimeError(f"Another `hubzoid run` is already running this deployment "
                               f"({self.state_dir}). Use `hubzoid status`.")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._lock_fh = fh

    def start_all(self, units: list[Unit]) -> dict[str, bool]:
        """First start: at most ``start_concurrency`` units at a time, each
        launched in order and then waited for together. A unit that doesn't
        come up is retried with backoff; the others carry on."""
        results: dict[str, bool] = {}
        for i in range(0, len(units), self.start_concurrency):
            batch = units[i:i + self.start_concurrency]
            launched = [u for u in batch if self.launch(u)]
            threads = [threading.Thread(target=lambda u=u: results.__setitem__(u.name, self.await_ready(u)),
                                        daemon=True) for u in launched]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for u in batch:
                results.setdefault(u.name, False)
        return results

    def serve(self, *, interval: float = 0.5) -> None:
        """Watch the units until this process is told to stop (SIGTERM/SIGINT
        raise SystemExit through the CLI's handlers), then stop them all: hubs
        first, the edge last."""
        self._write_table()
        self._serve_control()
        try:
            while not self._stop.is_set():
                self.check_once()
                time.sleep(interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        for folder in [self.state_dir, *self.pointers]:
            try:
                control_file(folder).unlink()
            except OSError:
                pass
        hubs = [u for u in self.units.values() if u.kind != "edge"]
        edges = [u for u in self.units.values() if u.kind == "edge"]
        threads = [threading.Thread(target=stop_procs, args=(u.procs,), daemon=True) for u in hubs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for u in edges:
            stop_procs(u.procs)


def _probe(unit: Unit) -> bool:
    import httpx

    if not unit.health_url:
        return True
    try:
        r = httpx.get(unit.health_url, timeout=2.0)
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    if not unit.identity:
        return True
    try:
        body = r.json()
    except ValueError:
        return False
    return (body.get("hub") == unit.identity.get("expected_hub")
            and unit.identity.get("expected_model") in (None, body.get("model")))
