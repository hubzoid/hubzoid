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
        # enhanced-telemetry beta + the traces exporter; metrics/logs alone do
        # NOT populate Langfuse.
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
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
