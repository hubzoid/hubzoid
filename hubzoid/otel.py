"""OpenTelemetry wiring for Hubzoid (opt-in, off by default).

Emission is rented from the SDKs: Claude Code emits native OTel when the
`OTEL_*` env is set on its subprocess; the OpenAI path emits via LiteLLM's
`otel` callback. Hubzoid only builds the env / attributes and picks the
destination. No bespoke capture. See memory `observability-otel-langfuse-plan`.
"""
from __future__ import annotations

import os


def _attr(value) -> str:
    """Sanitize a resource-attribute value so it can't corrupt the
    comma-and-equals-delimited OTEL_RESOURCE_ATTRIBUTES format."""
    s = "" if value is None else str(value)
    for ch in (",", "=", "\n", "\r", "\t"):
        s = s.replace(ch, "_")
    return s.strip() or "unknown"


def claude_otel_env(*, endpoint, user, hub, surface) -> dict[str, str]:
    """Env to inject into the `claude` subprocess so Claude Code emits OTel
    tagged with this turn's hubzoid.user / hubzoid.hub / hubzoid.surface.

    Returns an empty dict when no endpoint is configured (opt-in, off by
    default): nothing is emitted and the subprocess env is unchanged."""
    if not endpoint:
        return {}
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        # Trace spans (interaction -> llm_request -> tool) carry token+cost and
        # are what trace-based backends like Langfuse ingest. They require the
        # enhanced-telemetry beta + the traces exporter.
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        # Metrics and logs are OFF by default: a trace backend (Langfuse) drops
        # them, so emitting them is wasted egress (and logs can carry prompt
        # content). Token + cost already ride the trace spans. An operator with a
        # metrics/log backend opts in by setting these in the env (their value
        # wins).
        "OTEL_METRICS_EXPORTER": os.environ.get("OTEL_METRICS_EXPORTER") or "none",
        "OTEL_LOGS_EXPORTER": os.environ.get("OTEL_LOGS_EXPORTER") or "none",
        # OTLP/HTTP (what Langfuse and standard collectors accept on :4318).
        # Without an explicit protocol the exporter defaults to gRPC and silently
        # sends nothing to an HTTP endpoint. An operator-set value (a gRPC-only
        # collector) wins over our default.
        "OTEL_EXPORTER_OTLP_PROTOCOL": (
            os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf"
        ),
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        # user.id mirrors hubzoid.user so backends' native user views (e.g.
        # Langfuse's Users tab) attribute to the real OWUI person rather than
        # the shared claude-login subscription account.
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"hubzoid.user={_attr(user)},"
            f"hubzoid.hub={_attr(hub)},"
            f"hubzoid.surface={_attr(surface)},"
            f"user.id={_attr(user)}"
        ),
    }


def _merge_resource_attr(existing: str | None, extra: dict[str, str]) -> str:
    """Merge `extra` into a comma-delimited OTEL_RESOURCE_ATTRIBUTES string."""
    pairs: dict[str, str] = {}
    for kv in (existing or "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k.strip():
                pairs[k.strip()] = v
    pairs.update(extra)
    return ",".join(f"{k}={v}" for k, v in pairs.items())


# ---------------------------------------------------------------------------
# In-bridge OTLP normalize intercept (claude-local cost fix, opt-in).
#
# Claude Code puts token counts on trace spans under NON-standard names and
# stamps its own subscription account hash as the span user.id. Langfuse maps
# usage only from the semantic-convention `gen_ai.usage.*` names and keys its
# Users tab on span user.id, so a claude-local hub pointed straight at Langfuse
# shows traces but $0 cost and the wrong user. The stock otel-collector
# (docs/otel-collector.yaml) fixes this by renaming the attrs; this does the
# SAME rename+promotion INSIDE the bridge process so no extra collector has to
# run. Opt-in via HUBZOID_OTEL_NORMALIZE; claude-local only (a no-op on the
# LiteLLM path, which already emits the standard names).
# ---------------------------------------------------------------------------
_TOKEN_RENAME = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read_input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation_input_tokens",
}


def normalize_otlp_traces(body: bytes) -> bytes:
    """Rewrite an OTLP/HTTP ExportTraceServiceRequest so Langfuse maps it.

    Three changes, all no-ops on the LiteLLM path:
      * copy claude-local's token attrs to the `gen_ai.usage.*` names Langfuse
        maps (copy, not move, so it's idempotent and non-destructive);
      * set each span's `user.id` from the resource's `hubzoid.user` so
        Langfuse's Users tab attributes to the real OWUI person, not Claude
        Code's account hash;
      * promote the resource's `hubzoid.hub` to a Langfuse tag
        (`langfuse.trace.tags`) so per-hub cost is a first-class group-able
        dimension (Langfuse can't group by a custom metadata field).

    Best-effort: any parse failure returns the input unchanged (telemetry must
    never break the turn that produced it)."""
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except ImportError:
        return body
    try:
        req = trace_service_pb2.ExportTraceServiceRequest()
        req.ParseFromString(body)
        for rs in req.resource_spans:
            hub_user = None
            hub_name = None
            for a in rs.resource.attributes:
                if a.key == "hubzoid.user":
                    hub_user = a.value.string_value
                elif a.key == "hubzoid.hub":
                    hub_name = a.value.string_value
            for ss in rs.scope_spans:
                for span in ss.spans:
                    _normalize_span(span, hub_user, hub_name)
        return req.SerializeToString()
    except Exception:  # noqa: BLE001 — never let telemetry rewriting raise
        return body


def _normalize_span(span, hub_user: str | None, hub_name: str | None = None) -> None:
    keys = {a.key for a in span.attributes}
    # Snapshot the copies to add BEFORE mutating the repeated field.
    additions: list[tuple[str, object]] = []
    for a in span.attributes:
        dst = _TOKEN_RENAME.get(a.key)
        if dst and dst not in keys:
            from opentelemetry.proto.common.v1 import common_pb2
            av = common_pb2.AnyValue()
            av.CopyFrom(a.value)
            additions.append((dst, av))
    for dst, av in additions:
        kv = span.attributes.add()
        kv.key = dst
        kv.value.CopyFrom(av)
    if hub_user:
        existing = next((a for a in span.attributes if a.key == "user.id"), None)
        if existing is None:
            existing = span.attributes.add()
            existing.key = "user.id"
        existing.value.string_value = hub_user
    if hub_name:
        # Langfuse reads tags from `langfuse.trace.tags` (a string array).
        # Overwrite (Clear + set) so re-running is idempotent, not additive.
        tag = next((a for a in span.attributes if a.key == "langfuse.trace.tags"), None)
        if tag is None:
            tag = span.attributes.add()
            tag.key = "langfuse.trace.tags"
        tag.value.Clear()
        tag.value.array_value.values.add().string_value = hub_name


def parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS (`k=v,k=v`) into a dict.

    Split each pair on the FIRST `=` only: a Basic-auth value is base64 that can
    end in `=` padding, which must be preserved."""
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def local_intercept_endpoint(port: int) -> str:
    """The bridge's own loopback OTLP endpoint (the claude subprocess points
    here when normalize is on; the OTLP exporter appends `/v1/traces`)."""
    return f"http://127.0.0.1:{port}/otel"


def claude_export_endpoint(*, otel_endpoint, normalize: bool, bridge_port: int):
    """Where the claude subprocess should send OTel:
      * no endpoint            -> None (telemetry off);
      * normalize on           -> the bridge's local intercept (which rewrites
                                  and forwards to `otel_endpoint`);
      * normalize off          -> `otel_endpoint` directly.
    """
    if not otel_endpoint:
        return None
    return local_intercept_endpoint(bridge_port) if normalize else otel_endpoint


async def forward_otlp(body: bytes, *, forward_url: str, headers: dict,
                       client, gzipped: bool = False) -> int:
    """Normalize an OTLP trace batch and POST it upstream (e.g. to Langfuse).

    `client` is an httpx-style async client (injected so it can be faked in
    tests). Returns the upstream status code."""
    if gzipped:
        import gzip
        body = gzip.decompress(body)
    body = normalize_otlp_traces(body)
    out_headers = {"Content-Type": "application/x-protobuf", **(headers or {})}
    resp = await client.post(forward_url, content=body, headers=out_headers)
    return resp.status_code


def openai_otel_setup(*, endpoint, hub) -> bool:
    """Enable OTel on the OpenAI/LiteLLM path via LiteLLM's native `otel`
    callback (tokens + gen_ai.cost.total_cost). Process-level, so it carries
    hubzoid.hub (one bridge process per hub). Per-user on this path is a
    follow-up. Returns True when the callback is registered, else False.

    Off by default: no endpoint -> no-op.
    """
    if not endpoint:
        return False
    # Import first so we don't leave OTel env set on a process where the OpenAI
    # backend (and thus LiteLLM) isn't actually present.
    try:
        import litellm
    except ImportError:
        return False
    os.environ["OTEL_EXPORTER"] = "otlp_http"
    os.environ["OTEL_ENDPOINT"] = endpoint
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = _merge_resource_attr(
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES"), {"hubzoid.hub": _attr(hub)}
    )
    callbacks = list(getattr(litellm, "callbacks", None) or [])
    if "otel" not in callbacks:
        callbacks.append("otel")
        litellm.callbacks = callbacks
    return True
