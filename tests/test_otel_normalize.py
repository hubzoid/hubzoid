"""Tests for the in-bridge OTLP normalize intercept (claude-local cost fix).

Claude Code puts token counts on trace spans under NON-standard names
(`input_tokens`/`output_tokens`/`cache_*_tokens`) and stamps its own account
hash as the span `user.id`. Langfuse maps usage only from the semantic-
convention `gen_ai.usage.*` names and keys its Users tab on span `user.id`, so
pointed straight at Langfuse a claude-local hub shows traces but $0 cost and the
wrong user. The external collector (docs/otel-collector.yaml) fixes this; this
feature does the same rename+promotion INSIDE the bridge so no extra process is
needed. Opt-in via HUBZOID_OTEL_NORMALIZE, claude-local only, default off.
"""
from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from hubzoid import otel

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# --- OTLP protobuf helpers --------------------------------------------------
def _kv(key, value):
    av = common_pb2.AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    else:
        av.string_value = str(value)
    return common_pb2.KeyValue(key=key, value=av)


def _request(*, resource_attrs=None, span_attrs=None) -> bytes:
    span = trace_pb2.Span(name="llm_request")
    for k, v in (span_attrs or {}).items():
        span.attributes.append(_kv(k, v))
    res = resource_pb2.Resource()
    for k, v in (resource_attrs or {}).items():
        res.attributes.append(_kv(k, v))
    rs = trace_pb2.ResourceSpans(
        resource=res, scope_spans=[trace_pb2.ScopeSpans(spans=[span])]
    )
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[rs]
    ).SerializeToString()


def _span_attrs(body: bytes) -> dict:
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(body)
    span = req.resource_spans[0].scope_spans[0].spans[0]
    out = {}
    for a in span.attributes:
        which = a.value.WhichOneof("value")
        out[a.key] = getattr(a.value, which) if which else None
    return out


# --- normalize_otlp_traces: token-attr rename -------------------------------
def test_renames_claude_token_attrs_to_semconv():
    body = _request(span_attrs={
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_tokens": 10, "cache_creation_tokens": 5,
    })
    attrs = _span_attrs(otel.normalize_otlp_traces(body))
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 50
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 10
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 5


def test_noop_on_litellm_standard_names():
    # The OpenAI/LiteLLM path already emits gen_ai.usage.* — there are no claude
    # names to rename, so one intercept can serve every backend harmlessly.
    body = _request(span_attrs={"gen_ai.usage.input_tokens": 7})
    attrs = _span_attrs(otel.normalize_otlp_traces(body))
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert "input_tokens" not in attrs


def test_idempotent_when_run_twice():
    body = _request(span_attrs={"input_tokens": 100})
    once = otel.normalize_otlp_traces(body)
    twice = otel.normalize_otlp_traces(once)
    assert _span_attrs(twice)["gen_ai.usage.input_tokens"] == 100


# --- normalize_otlp_traces: user.id promotion -------------------------------
def test_promotes_user_id_from_resource_hubzoid_user():
    body = _request(
        resource_attrs={"hubzoid.user": "priya@x.org"},
        span_attrs={"input_tokens": 1},
    )
    assert _span_attrs(otel.normalize_otlp_traces(body))["user.id"] == "priya@x.org"


def test_overwrites_claude_account_hash_user_id():
    # Claude Code stamps its subscription account hash as span user.id; the real
    # OWUI person is in resource hubzoid.user. Langfuse's Users tab keys on span
    # user.id, so we overwrite it with the real identity.
    body = _request(
        resource_attrs={"hubzoid.user": "priya@x.org"},
        span_attrs={"user.id": "acct_deadbeef"},
    )
    assert _span_attrs(otel.normalize_otlp_traces(body))["user.id"] == "priya@x.org"


def test_leaves_user_id_when_no_hubzoid_user_in_resource():
    body = _request(span_attrs={"user.id": "acct_abc"})
    assert _span_attrs(otel.normalize_otlp_traces(body))["user.id"] == "acct_abc"


# --- normalize_otlp_traces: hub -> Langfuse tag -----------------------------
def _span_tags(body: bytes):
    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(body)
    span = req.resource_spans[0].scope_spans[0].spans[0]
    for a in span.attributes:
        if a.key == "langfuse.trace.tags":
            return [v.string_value for v in a.value.array_value.values]
    return None


def test_promotes_hub_to_langfuse_tag():
    # hubzoid.hub is only metadata (Langfuse can't group by it); also emit it as
    # a Langfuse tag (`langfuse.trace.tags`, a string array) so per-hub cost is a
    # first-class group-able dimension.
    body = _request(resource_attrs={"hubzoid.hub": "gpms-hub"}, span_attrs={"input_tokens": 1})
    assert _span_tags(otel.normalize_otlp_traces(body)) == ["gpms-hub"]


def test_no_hub_tag_when_no_hub_in_resource():
    body = _request(span_attrs={"input_tokens": 1})
    assert _span_tags(otel.normalize_otlp_traces(body)) is None


def test_hub_tag_is_idempotent():
    body = _request(resource_attrs={"hubzoid.hub": "irs-hub"}, span_attrs={"input_tokens": 1})
    twice = otel.normalize_otlp_traces(otel.normalize_otlp_traces(body))
    assert _span_tags(twice) == ["irs-hub"]  # not ["irs-hub", "irs-hub"]


# --- normalize_otlp_traces: robustness --------------------------------------
def test_returns_input_unchanged_on_unparseable_bytes():
    junk = b"not a protobuf \xff\x00\x01"
    assert otel.normalize_otlp_traces(junk) == junk


# --- OTLP header parsing ----------------------------------------------------
def test_parse_headers_keeps_base64_padding():
    # OTEL_EXPORTER_OTLP_HEADERS is comma-separated k=v; a Basic-auth value is
    # base64 that can END in '=' padding, so split on the FIRST '=' only.
    raw = "Authorization=Basic dXNlcjpwYXNz=="
    assert otel.parse_otlp_headers(raw) == {"Authorization": "Basic dXNlcjpwYXNz=="}


def test_parse_headers_multiple_and_empty():
    assert otel.parse_otlp_headers("A=1,B=2") == {"A": "1", "B": "2"}
    assert otel.parse_otlp_headers(None) == {}
    assert otel.parse_otlp_headers("") == {}


# --- endpoint helpers -------------------------------------------------------
def test_local_intercept_endpoint():
    assert otel.local_intercept_endpoint(8000) == "http://127.0.0.1:8000/otel"


def test_claude_export_endpoint_direct_when_normalize_off():
    assert otel.claude_export_endpoint(
        otel_endpoint="https://lf/api/public/otel", normalize=False, bridge_port=8000
    ) == "https://lf/api/public/otel"


def test_claude_export_endpoint_local_when_normalize_on():
    assert otel.claude_export_endpoint(
        otel_endpoint="https://lf/api/public/otel", normalize=True, bridge_port=8123
    ) == "http://127.0.0.1:8123/otel"


def test_claude_export_endpoint_none_when_no_endpoint():
    # Normalize is meaningless with no backend to send to — stays off.
    assert otel.claude_export_endpoint(
        otel_endpoint=None, normalize=True, bridge_port=8000
    ) is None


# --- forward_otlp: normalize + forward with an injected client --------------
class _FakeResp:
    status_code = 200


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, *, content, headers):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResp()


def test_forward_normalizes_body_and_sends_headers():
    body = _request(
        resource_attrs={"hubzoid.user": "priya@x"},
        span_attrs={"input_tokens": 100, "user.id": "acct_hash"},
    )
    client = _FakeClient()
    status = asyncio.run(otel.forward_otlp(
        body, forward_url="https://lf/api/public/otel/v1/traces",
        headers={"Authorization": "Basic abc"}, client=client,
    ))
    assert status == 200
    call = client.calls[0]
    assert call["url"] == "https://lf/api/public/otel/v1/traces"
    assert call["headers"]["Authorization"] == "Basic abc"
    assert call["headers"]["Content-Type"] == "application/x-protobuf"
    attrs = _span_attrs(call["content"])
    assert attrs["gen_ai.usage.input_tokens"] == 100  # renamed before forward
    assert attrs["user.id"] == "priya@x"               # promoted before forward


def test_forward_gunzips_before_normalizing():
    raw = _request(span_attrs={"input_tokens": 42})
    client = _FakeClient()
    asyncio.run(otel.forward_otlp(
        gzip.compress(raw), forward_url="u", headers={}, client=client, gzipped=True,
    ))
    assert _span_attrs(client.calls[0]["content"])["gen_ai.usage.input_tokens"] == 42


# --- settings seam ----------------------------------------------------------
def test_settings_reads_otel_normalize_flag(tmp_path, monkeypatch):
    from hubzoid import settings as settingslib
    monkeypatch.setenv("HUBZOID_OTEL_NORMALIZE", "1")
    assert settingslib.load(tmp_path).otel_normalize is True


def test_settings_otel_normalize_defaults_false(tmp_path, monkeypatch):
    from hubzoid import settings as settingslib
    monkeypatch.delenv("HUBZOID_OTEL_NORMALIZE", raising=False)
    assert settingslib.load(tmp_path).otel_normalize is False


# --- claude runtime points the subprocess at the local intercept ------------
def test_build_claude_runtime_redirects_export_to_local_intercept(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", "https://lf/api/public/otel")
    monkeypatch.setenv("HUBZOID_OTEL_NORMALIZE", "1")
    monkeypatch.setenv("BRIDGE_PORT", "8000")
    from hubzoid.factory_claude import build_claude_runtime
    rt = build_claude_runtime(MINIMAL)
    assert rt._otel_endpoint == "http://127.0.0.1:8000/otel"


def test_build_claude_runtime_direct_when_normalize_off(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", "https://lf/api/public/otel")
    monkeypatch.delenv("HUBZOID_OTEL_NORMALIZE", raising=False)
    from hubzoid.factory_claude import build_claude_runtime
    rt = build_claude_runtime(MINIMAL)
    assert rt._otel_endpoint == "https://lf/api/public/otel"


# --- bridge route: /otel/v1/traces normalizes and forwards ------------------
def _client(monkeypatch, *, model="claude-local", normalize=True):
    from fastapi.testclient import TestClient

    from hubzoid.server import build_app
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(MINIMAL))
    monkeypatch.setenv("MODEL", model)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("BRIDGE_PORT", "8000")
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", "https://lf.example/api/public/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc123")
    if normalize:
        monkeypatch.setenv("HUBZOID_OTEL_NORMALIZE", "1")
    else:
        monkeypatch.delenv("HUBZOID_OTEL_NORMALIZE", raising=False)
    return TestClient(build_app())


def test_bridge_route_forwards_normalized_traces(monkeypatch):
    captured = {}

    async def fake_forward(body, *, forward_url, headers, client, gzipped=False):
        captured["forward_url"] = forward_url
        captured["headers"] = headers
        captured["body"] = body
        captured["gzipped"] = gzipped
        return 200

    monkeypatch.setattr("hubzoid.otel.forward_otlp", fake_forward)
    client = _client(monkeypatch)

    body = _request(
        resource_attrs={"hubzoid.user": "priya@x"}, span_attrs={"input_tokens": 9}
    )
    r = client.post("/otel/v1/traces", content=body,
                    headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 200
    assert captured["forward_url"] == "https://lf.example/api/public/otel/v1/traces"
    assert captured["headers"]["Authorization"] == "Basic abc123"
    assert captured["body"] == body


def test_bridge_route_absent_when_normalize_off(monkeypatch):
    client = _client(monkeypatch, normalize=False)
    r = client.post("/otel/v1/traces", content=b"x")
    assert r.status_code == 404


def test_bridge_route_absent_on_non_claude_backend(monkeypatch):
    # The OpenAI/LiteLLM path emits standard names already; no intercept needed.
    client = _client(monkeypatch, model="openrouter/anthropic/claude-haiku-4.5")
    r = client.post("/otel/v1/traces", content=b"x")
    assert r.status_code == 404
