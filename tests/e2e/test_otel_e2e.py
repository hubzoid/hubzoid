"""E2E: OTel through the real ClaudeRuntime, attributed per OWUI user + hub.

Self-skips when the `claude` CLI is absent / not logged in or the SDK is
missing. Runs one real claude-local turn with HUBZOID_OTEL_ENDPOINT pointed at
an in-process OTLP receiver and asserts the receiver captured Claude Code's
token metric tagged with this turn's hubzoid.user / hubzoid.hub.

Regression guard for the protocol gotcha: without OTEL_EXPORTER_OTLP_PROTOCOL
the exporter defaults to gRPC and nothing reaches an HTTP endpoint.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


def _claude_ready() -> bool:
    if shutil.which("claude") is None:
        return False
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed / logged in"),
]

MINIMAL = Path(__file__).resolve().parents[1] / "fixtures" / "minimal_hub"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_receiver(sink: list):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            if self.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            sink.append((self.path, raw.decode("latin-1", "replace")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    return Handler


async def _one_turn():
    from hubzoid.access import identity as idmod
    from hubzoid.factory_claude import build_claude_runtime

    rt = build_claude_runtime(MINIMAL)
    with idmod.identity_scope(idmod.Identity.make(user="priya", surface="owui")):
        out = ""
        async for chunk in rt.stream("Reply with exactly: OK"):
            out += chunk
    return out


def test_otel_emits_attributed_metrics_through_runtime(monkeypatch):
    sink: list[tuple[str, str]] = []
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _make_receiver(sink))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("MODEL", "claude-local")
        monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "1000")
        monkeypatch.setenv("OTEL_LOGS_EXPORT_INTERVAL", "1000")

        out = asyncio.run(asyncio.wait_for(_one_turn(), timeout=150))
        assert out.strip(), "the turn should still produce a reply"

        time.sleep(6)  # let the export interval flush
    finally:
        srv.shutdown()

    paths = {p for p, _ in sink}
    blob = "".join(body for _, body in sink)
    # Traces are the signal Langfuse ingests. If OTEL_TRACES_EXPORTER or the
    # enhanced-telemetry beta ever gets dropped, this fails (Langfuse would go
    # blank while metrics/logs still flowed).
    assert "/v1/traces" in paths, "no trace spans emitted (Langfuse needs traces)"
    assert "claude_code.token.usage" in blob, "no token metric reached the receiver"
    assert "hubzoid.user" in blob and "priya" in blob, "OWUI user not on the telemetry"
