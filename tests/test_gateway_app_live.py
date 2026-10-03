"""`hubzoid gateway` for real, in the web app mode: two bridges and the edge
as processes on loopback ports 3690-3699 (no Open WebUI, no model call).

Checks through the edge: the deployment-wide /api/agents lists each agent the
signed-in person may use (local mode: the local owner), hub-scoped calls under
/b/<slug>/api reach that hub's bridge, both bridges decide from one shared
store (a grant written anywhere applies everywhere, and the local owner is
provisioned on every hub at the first start),
branding comes from the gateway folder and each hub's own under /b/<slug>,
and the bridge's internal API stays off the public port."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.slow  # starts a live gateway and its bridges

from hubzoid.access.store import GrantStore

REPO = Path(__file__).resolve().parents[1]
EDGE, SALES, SUPPORT = 3691, 3692, 3693
OWNER = "admin@localhost"
# Generous: these hosts often run several test suites at once.
TIMEOUT = 60


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _hub(root: Path, name: str, port: int) -> Path:
    hub = root / name
    hub.mkdir()
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: The {name} agent\n---\nYou help {name}.\n")
    (hub / ".env").write_text(
        f"BRIDGE_PORT={port}\nMODEL=openrouter/anthropic/claude-haiku-4.5\n"
        f"OPENROUTER_API_KEY=test-key\nBRIDGE_API_KEYS=k-{name}-0123456789\nHUB_LOG_LEVEL=warning\n")
    return hub


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if not all(_free(p) for p in (EDGE, SALES, SUPPORT)):
        pytest.skip("ports 3691-3693 are busy")
    root = tmp_path_factory.mktemp("gw-live")
    sales, support = _hub(root, "sales", SALES), _hub(root, "support", SUPPORT)
    (sales / "branding").mkdir()
    (sales / "branding" / "logo.png").write_bytes(b"sales-logo")
    gw = root / "gw"
    (gw / "branding").mkdir(parents=True)
    (gw / "branding" / "logo.svg").write_bytes(b"<svg>acme</svg>")
    # The shared store, as a gateway leaves it: both hubs managed in the
    # Console; the local owner administers and may use sales only.
    store = GrantStore(__import__("sqlalchemy").create_engine(f"sqlite:///{gw / 'hubzoid-operational.db'}"))
    store.bootstrap([OWNER])
    store.grant(OWNER, "sales", "use_hub", actor="test")

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "USER")}
    env.update(PYTHONPATH=str(REPO), HUBZOID_AUTH="false", HUB_LOG_LEVEL="warning")
    log = open(root / "gateway.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "hubzoid", "gateway", str(sales), str(support), "--port", str(EDGE),
         "--data-dir", str(gw), "--name", "Acme"],
        cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{EDGE}"
    try:
        deadline = time.time() + 400   # a busy host imports slowly
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if proc.poll() is not None or httpx.get(base + "/healthz", timeout=5).status_code != 200:
            log.flush()
            pytest.fail("gateway did not start:\n" + (root / "gateway.log").read_text()[-4000:])
        yield {"base": base, "store": store, "root": root, "gw": gw}
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        log.close()
        deadline = time.time() + 15
        while time.time() < deadline and not all(_free(p) for p in (EDGE, SALES, SUPPORT)):
            time.sleep(0.5)


@contextlib.contextmanager
def _diagnose(live):
    """On failure, show what the gateway and its bridges logged."""
    try:
        yield
    except (AssertionError, httpx.HTTPError) as exc:
        tail = (live["root"] / "gateway.log").read_text(errors="replace")[-4000:]
        raise AssertionError(f"{exc!r}\n--- gateway log (tail) ---\n{tail}") from exc


def _agents(base: str, path: str = "/api/agents") -> list[str]:
    r = httpx.get(base + path, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return [a["id"] for a in r.json()["agents"]]


def test_agents_across_the_gateway_follow_one_shared_store(live):
    with _diagnose(live):
        base, store = live["base"], live["store"]
        # The local owner owns every hub from the first start.
        body = httpx.get(base + "/api/agents", timeout=TIMEOUT).json()
        assert [(a["id"], a["api_base"]) for a in body["agents"]] == [
            ("sales", "/b/sales"), ("support", "/b/support")]
        assert body["agents"][0]["avatar_url"] == "/b/sales/branding/logo.png"
        # A revoke written here applies on every bridge at once.
        store.revoke(OWNER, "support", "use_hub", actor="test")
        assert _agents(base) == ["sales"]
        assert _agents(base, "/b/support/api/agents") == ["sales"]
        store.grant(OWNER, "support", "use_hub", actor="test")
        assert _agents(base, "/b/sales/api/agents") == ["sales", "support"]


def test_branding_through_the_edge(live):
    with _diagnose(live):
        base = live["base"]
        assert httpx.get(base + "/api/branding", timeout=TIMEOUT).json() == {
            "name": "Acme", "logo_url": "/branding/logo.svg", "favicon_url": "/branding/logo.svg",
            "custom_css_url": None}
        assert httpx.get(base + "/branding/logo.svg", timeout=TIMEOUT).content == b"<svg>acme</svg>"
        assert httpx.get(base + "/b/sales/branding/logo.png", timeout=TIMEOUT).content == b"sales-logo"
        hub = httpx.get(base + "/b/sales/api/branding", timeout=TIMEOUT).json()
        assert hub["name"] == "sales" and hub["logo_url"] == "/b/sales/branding/logo.png"
        assert httpx.get(base + "/b/support/branding/logo.png", timeout=TIMEOUT).status_code == 404


def test_the_bridge_api_stays_on_loopback(live):
    with _diagnose(live):
        base = live["base"]
        r = httpx.post(base + "/v1/chat/completions", timeout=TIMEOUT,
                       headers={"Authorization": "Bearer k-sales-0123456789",
                                "X-OpenWebUI-User-Email": OWNER},
                       json={"model": "sales", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 404
        assert httpx.get(base + "/v1/models", timeout=TIMEOUT).status_code == 404
        # On loopback the bridge still answers its own callers.
        r = httpx.get(f"http://127.0.0.1:{SALES}/v1/models", timeout=TIMEOUT,
                      headers={"Authorization": "Bearer k-sales-0123456789"})
        assert r.status_code == 200 and r.json()["data"][0]["id"] == "sales"


def test_the_manifest_and_key_describe_the_deployment(live):
    with _diagnose(live):
        import json

        gw = live["gw"]
        manifest = json.loads((gw / "deployment.json").read_text())
        assert manifest["ui_mode"] == "hubzoid" and manifest["auth"] is False
        assert [h["slug"] for h in manifest["hubs"]] == ["sales", "support"]
        assert (gw / "secret.key").is_file()


@pytest.mark.parametrize('journey', ['standalone-edge-exit', 'gateway-startup-stop'])
def test_service_failure_and_startup_stop_release_owned_processes(tmp_path, journey):
    """Exercise the real CLI and uvicorn children, without a provider call."""
    ports = []
    holders = []
    for _ in range(3):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        holders.append(sock)
        ports.append(sock.getsockname()[1])
    for sock in holders:
        sock.close()
    edge, first, second = ports
    hubs = [_hub(tmp_path, name, port) for name, port in [('one', first), ('two', second)]]
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER')}
    env.update(PYTHONPATH=str(REPO), MODEL='hubzoid-test/scripted', HUBZOID_TEST_RUNTIME='1',
               HUBZOID_AUTH='false', HUBZOID_DISABLE_SCHEDULE='true', MCP_SERVER='false')
    # Hub .env wins over process defaults; use the scripted adapter explicitly.
    for hub, port in zip(hubs, [first, second]):
        (hub / '.env').write_text(f'MODEL=hubzoid-test/scripted\nBRIDGE_PORT={port}\n'
                                  'HUBZOID_TEST_RUNTIME=1\nBRIDGE_API_KEYS=local-review-key\n')
    marker = tmp_path / 'children.jsonl'
    harness = '''import json, subprocess, sys, time
from pathlib import Path
from hubzoid import cli
original = subprocess.Popen
marker = Path(sys.argv[1])
class Record(original):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        with marker.open('a') as f:
            f.write(json.dumps({'pid': self.pid, 'command': a[0]}) + '\\n')
subprocess.Popen = Record
if sys.argv[2] == 'gateway-startup-stop':
    # Hold readiness while a real headless bridge starts. SIGTERM must unwind
    # this startup wait and clean up that bridge's process group.
    cli._wait_for_bridge = lambda *a, **kw: time.sleep(120)
sys.argv = [sys.argv[0], *sys.argv[3:]]
cli.app()
'''
    args = (['run', str(hubs[0]), '--port', str(edge), '--bridge-port', str(first), '--no-open']
            if journey == 'standalone-edge-exit' else
            ['gateway', *map(str, hubs), '--port', str(edge), '--data-dir', str(tmp_path / 'gateway')])
    log_path = tmp_path / 'process.log'
    with log_path.open('w') as log:
        proc = subprocess.Popen([sys.executable, '-c', harness, str(marker), journey, *args],
                                cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        children = []
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                children = [json.loads(line) for line in marker.read_text().splitlines()] if marker.exists() else []
                assert proc.poll() is None, log_path.read_text()[-4000:]
                if journey == 'gateway-startup-stop' and children:
                    try:
                        if httpx.get(f'http://127.0.0.1:{first}/healthz', timeout=.5).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                if journey == 'standalone-edge-exit' and len(children) == 2:
                    try:
                        if httpx.get(f'http://127.0.0.1:{edge}/healthz', timeout=.5).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(.1)
            else:
                pytest.fail('Startup timed out: ' + log_path.read_text()[-4000:])
            if journey == 'standalone-edge-exit':
                os.kill(children[-1]['pid'], signal.SIGTERM)
                expected = 1
            else:
                os.kill(proc.pid, signal.SIGTERM)
                expected = 0
            assert proc.wait(timeout=30) == expected, log_path.read_text()[-4000:]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(_free(port) for port in ports):
                    break
                time.sleep(.1)
            assert all(_free(port) for port in ports), log_path.read_text()[-4000:]
            for child in children:
                # No children in the owned groups, even if their leader exited.
                with pytest.raises(ProcessLookupError):
                    os.killpg(child['pid'], 0)
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGTERM)
                proc.wait(timeout=30)
            for child in children:
                try:
                    os.killpg(child['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
