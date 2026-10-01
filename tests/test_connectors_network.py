"""Outbound requests for connectors (``hubzoid.connectors.http``): where a
remote server may send Hubzoid, deadlines, and the probe's session cleanup.

A registered MCP server names every other URL Hubzoid fetches (its metadata,
its authorization server's endpoints). Name resolution is scripted here
(``socket.getaddrinfo``) and every connection Hubzoid opens is recorded
(``socket.create_connection``): only loopback connections really happen, so
nothing leaves this computer, and a refused address is never even dialled.
"""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from hubzoid.connectors import ConnectorError, discovery
from hubzoid.connectors import http as net
from tests import connectors_fakes as f

# Scripted DNS: these names never reach a real resolver.
NAMES = {
    "internal.example.org": ["10.0.0.5"],
    "metadata.example.org": ["169.254.169.254"],
    "mapped-metadata.example.org": ["::ffff:169.254.169.254"],
    "nat64-metadata.example.org": ["64:ff9b::a9fe:a9fe"],
    "mixed.example.org": ["93.184.215.14", "10.0.0.9"],
    "public.example.org": ["93.184.215.14"],
    "mcp.corp.example": ["10.0.0.5"],
    "sso.corp.example": ["10.0.0.6"],
    "multicast.example.org": ["224.0.0.1"],
}


@pytest.fixture
def network(monkeypatch):
    """Scripted names, and a record of every connection opened. Non-loopback
    connections are refused after being recorded (nothing leaves this computer)."""
    f.clean_env(monkeypatch)
    monkeypatch.delenv(net.PRIVATE_HOSTS_ENV, raising=False)
    real_resolve, real_connect = socket.getaddrinfo, socket.create_connection
    opened: list[tuple[str, int]] = []

    def resolve(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name in NAMES:
            out = []
            for addr in NAMES[name]:
                family = socket.AF_INET6 if ":" in addr else socket.AF_INET
                sockaddr = (addr, port or 0, 0, 0) if family == socket.AF_INET6 else (addr, port or 0)
                out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
            return out
        return real_resolve(host, port, *args, **kwargs)

    def connect(address, *args, **kwargs):
        host, port = address[0], address[1]
        opened.append((host, port))
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(address, *args, **kwargs)
        raise ConnectionRefusedError(f"test: {host} is not dialled")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    return opened


def remote(opened) -> list:
    """Connections that would have left this computer."""
    return [o for o in opened if o[0] not in ("127.0.0.1", "::1", "localhost")]


class _Mcp(BaseHTTPRequestHandler):
    """An MCP endpoint that asks for sign-in and names its resource metadata
    (``server.hint``); ``server.documents`` maps a path to a JSON body."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{self.server.hint}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        body = self.server.documents.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = body.replace("{origin}", self.server.origin).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def local_mcp():
    """A loopback MCP server (development: the connector is on this computer)."""
    port = f.free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Mcp)
    server.hint = ""
    server.documents = {}
    server.origin = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Review finding 1: remote metadata may not send Hubzoid into private networks
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hint", [
    "https://internal.example.org/admin",        # a name for a private address
    "https://10.0.0.5/admin",                    # a private address
    "https://metadata.example.org/latest/meta-data/",
    "https://169.254.169.254/latest/meta-data/",
    "https://mixed.example.org/admin",           # one public answer, one private
])
def test_a_registered_server_cannot_send_discovery_into_the_private_network(network, local_mcp,
                                                                            hint):
    local_mcp.hint = hint
    with pytest.raises(ConnectorError) as err:
        discovery.discover(f"{local_mcp.origin}/mcp")
    assert remote(network) == []  # never dialled
    assert err.value.code == "private_address"
    assert "HUBZOID_CONNECTOR_PRIVATE_HOSTS" in err.value.message or "metadata" in err.value.message


def test_an_authorization_server_on_a_private_network_needs_the_allowlist(network, local_mcp,
                                                                        monkeypatch):
    local_mcp.hint = f"{local_mcp.origin}/.well-known/oauth-protected-resource/mcp"
    local_mcp.documents["/.well-known/oauth-protected-resource/mcp"] = (
        '{"resource": "{origin}/mcp", "authorization_servers": ["https://sso.corp.example"]}')
    with pytest.raises(ConnectorError) as err:
        discovery.discover(f"{local_mcp.origin}/mcp")
    assert err.value.code == "private_address" and "sso.corp.example" in err.value.message
    assert remote(network) == []

    # The administrator lists their identity provider: Hubzoid may go there.
    monkeypatch.setenv(net.PRIVATE_HOSTS_ENV, "idp.other.example, sso.corp.example")
    with pytest.raises(ConnectorError):
        discovery.discover(f"{local_mcp.origin}/mcp")  # not answered here, but tried
    assert ("10.0.0.6", 443) in remote(network)


def test_the_address_rule_on_each_connection(network, monkeypatch):
    """Private addresses: the registered server's own host, or an allowlisted
    host. Cloud metadata: never. Every check uses the resolved addresses."""
    def tried(connector_url, url):
        network.clear()
        with net.client(connector_url) as c:
            with pytest.raises(httpx.HTTPError) as err:
                c.get(url)
        return remote(network), net.refused_host(err.value)

    private = "https://mcp.corp.example/mcp"  # an internal MCP server (10.0.0.5)
    # Its own host, any port: allowed (dialled, then refused by this test).
    assert tried(private, "https://mcp.corp.example:9443/token") == ([("10.0.0.5", 9443)], None)
    # Another private host, by name or by address: refused, never dialled.
    assert tried(private, "https://sso.corp.example/token") == ([], "sso.corp.example")
    assert tried(private, "https://internal.example.org/x") == ([], "internal.example.org")
    # A public server may not name private ones either.
    assert tried("https://public.example.org/mcp", "https://internal.example.org/x")[0] == []
    assert tried("https://public.example.org/mcp", "https://multicast.example.org/x")[0] == []
    # Public addresses are fine, and the connection goes to the address checked.
    assert tried(private, "https://public.example.org/x") == ([("93.184.215.14", 443)], None)
    # Cloud metadata, however written, never: not even on an allowlisted host.
    monkeypatch.setenv(net.PRIVATE_HOSTS_ENV, "metadata.example.org,mapped-metadata.example.org,"
                                              "nat64-metadata.example.org,sso.corp.example")
    for name in ("metadata.example.org", "mapped-metadata.example.org",
                 "nat64-metadata.example.org"):
        assert tried(private, f"https://{name}/latest")[0] == [], name
    assert tried(private, "https://sso.corp.example/token") == ([("10.0.0.6", 443)], None)
    # A connector on this computer (development) may use this computer, and
    # nothing else that is private.
    monkeypatch.delenv(net.PRIVATE_HOSTS_ENV)
    network.clear()
    with net.client("http://localhost:8000/mcp") as c:
        with pytest.raises(httpx.HTTPError) as err:
            c.get("http://127.0.0.1:9/token")  # nothing listens: refused by the OS
    assert ("127.0.0.1", 9) in network and net.refused_host(err.value) is None
    assert tried("http://localhost:8000/mcp", "https://internal.example.org/x") == \
        ([], "internal.example.org")


def test_a_name_is_resolved_once_and_the_checked_address_is_used(network, monkeypatch):
    """DNS rebinding: a name answering a public address to the check and a
    private one a moment later still gets only the public address dialled."""
    answers = iter([["93.184.215.14"], ["10.0.0.5"], ["10.0.0.5"], ["10.0.0.5"]])
    real = socket.getaddrinfo

    def rebinding(host, port, *args, **kwargs):
        if host == "rebind.example.org":
            (addr,) = next(answers)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    with net.client("https://public.example.org/mcp") as c:
        with pytest.raises(httpx.HTTPError):
            c.get("https://rebind.example.org/token")
    assert remote(network) == [("93.184.215.14", 443)]


def test_literal_addresses_in_discovered_urls_follow_the_same_rule(monkeypatch):
    monkeypatch.delenv(net.PRIVATE_HOSTS_ENV, raising=False)
    base = "https://mcp.example.org/mcp"
    for bad in ("https://10.0.0.5/token", "https://[fd00::5]/token", "https://192.168.1.2/t",
                "https://100.64.0.1/t", "https://169.254.169.254/latest"):
        with pytest.raises(ConnectorError) as err:
            net.check_url(bad, base=base)
        assert err.value.code == "private_address", bad
    # The registered server's own private host may name itself.
    assert net.check_url("https://10.0.0.5:8443/token", base="https://10.0.0.5/mcp")
    # Or the administrator allows a host.
    monkeypatch.setenv(net.PRIVATE_HOSTS_ENV, "10.0.0.5")
    assert net.check_url("https://10.0.0.5/token", base=base)
    with pytest.raises(ConnectorError):  # metadata stays refused
        net.check_url("https://169.254.169.254/x", base="https://169.254.169.254/mcp")


# ---------------------------------------------------------------------------
# Review finding 2: bounded probe cleanup and an overall deadline
# ---------------------------------------------------------------------------
def test_the_probe_closes_its_session_without_reading_the_answer(monkeypatch):
    """A server that opened a session for the probe answers the cleanup DELETE
    with an endless body: Hubzoid sends the DELETE and never reads it."""
    sent: list = []
    read: list[int] = []

    def endless():
        for i in range(64):  # 4 MiB, well past MAX_JSON_BYTES
            read.append(i)
            yield b"x" * 65536

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, headers={"mcp-session-id": "s-1"}, json={"result": {}})
        sent.append((request.method, request.headers.get("mcp-session-id")))
        return httpx.Response(200, content=endless())

    monkeypatch.setattr(net, "_transport", httpx.MockTransport(handler))
    with net.client() as c:
        pr = discovery.probe(c, "https://mcp.example.org/mcp")
    assert pr.requires_auth is False
    assert sent == [("DELETE", "s-1")]
    assert read == []


def _trickling_server(stop: threading.Event):
    """Answers every connection with a status line, then one header line every
    0.2 s for up to 8 s: each read is quick, the response never ends."""
    port = f.free_port()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(8)
    listener.settimeout(0.2)

    def serve(conn):
        with conn:
            conn.settimeout(1)
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n")
                end = time.monotonic() + 8
                n = 0
                while not stop.is_set() and time.monotonic() < end:
                    conn.sendall(b"X-Slow-%d: y\r\n" % n)
                    n += 1
                    time.sleep(0.2)
            except OSError:
                pass

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
        listener.close()

    threading.Thread(target=accept, daemon=True).start()
    return port


def test_discovery_has_an_overall_deadline(monkeypatch):
    f.clean_env(monkeypatch)
    monkeypatch.setattr(net, "DEADLINE", 1.0, raising=False)
    stop = threading.Event()
    port = _trickling_server(stop)
    began = time.monotonic()
    try:
        with pytest.raises(ConnectorError) as err:
            discovery.discover(f"http://127.0.0.1:{port}/mcp")
    finally:
        stop.set()
    assert time.monotonic() - began < 4
    assert err.value.code == "unreachable"


def test_a_refresh_cannot_outlive_its_lease_even_while_headers_trickle(monkeypatch):
    """The refresh deadline covers the whole request, not just the body, so no
    other process can take the lease while this one may still spend the token."""
    from hubzoid.connectors import tokens

    f.clean_env(monkeypatch)
    monkeypatch.setattr(tokens, "REFRESH_DEADLINE", 1.0)
    stop = threading.Event()
    port = _trickling_server(stop)
    began = time.monotonic()
    try:
        outcome, reason = tokens._post_refresh({
            "refresh_token": "rt", "token_endpoint": f"http://127.0.0.1:{port}/token",
            "client": {"client_id": "c"}, "url": f"http://127.0.0.1:{port}/mcp"})
    finally:
        stop.set()
    assert time.monotonic() - began < 4
    assert (outcome, reason) == ("unavailable", "ReadTimeout")
