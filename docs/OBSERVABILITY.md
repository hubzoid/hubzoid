# Observability (opt-in OTel)

Hubzoid emits OpenTelemetry from the model SDKs. It is **off by default**. Turn
it on per hub by setting one env var; point it at any OTLP/HTTP backend
(Langfuse recommended). There is no bespoke telemetry to maintain: Claude Code
and LiteLLM do the emitting, Hubzoid only injects the destination and the
per-turn attribution.

## Enable

Set the OTLP endpoint in the hub's `.env` (or the process env via systemd /
Docker):

```
HUBZOID_OTEL_ENDPOINT=https://langfuse.internal/api/public/otel
```

For a backend that needs auth (Langfuse uses HTTP Basic with its public/secret
keys), set the standard OTel header var alongside it. It flows straight through
to the exporter, no Hubzoid code involved:

```
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public_key:secret_key)>
```

Unset `HUBZOID_OTEL_ENDPOINT` (the default) and nothing is emitted.

On the **claude-local** path, also set this to get cost in Langfuse (see the
caveat below):

```
HUBZOID_OTEL_NORMALIZE=1
```

Only **traces** are emitted by default (metrics and logs are off): token and
cost ride the trace spans, a trace backend like Langfuse drops metrics/logs
anyway, and logs can carry prompt content. An operator with a metrics/log
backend opts in with `OTEL_METRICS_EXPORTER=otlp` / `OTEL_LOGS_EXPORTER=otlp`.

## What you get, and the one caveat (attribute names)

Every turn emits trace spans (`interaction -> llm_request -> tool`) tagged with
`hubzoid.user`, `hubzoid.hub`, `hubzoid.surface` (and `user.id` mirroring the
OWUI email) as **resource attributes** — so traces and per-hub / per-surface
filtering work on any OTLP backend, direct.

Whether **cost** shows depends on the model path, because of attribute naming:

- **OpenAI / LiteLLM path** (OpenRouter, Azure, OpenAI, ...): LiteLLM emits the
  standard `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` and
  `gen_ai.cost.total_cost`. Langfuse maps and prices these **natively — point
  `HUBZOID_OTEL_ENDPOINT` straight at Langfuse, cost works, no collector.**
- **claude-local path** (Claude Code): the token counts are on the spans but
  under the **non-standard** names `input_tokens` / `output_tokens` /
  `cache_*_tokens`, which Langfuse does not map — so token/cost show as **0**
  when pointed directly at Langfuse. The fix is one flag:

  ```
  HUBZOID_OTEL_NORMALIZE=1
  ```

  With it set, the bridge intercepts its own `claude` subprocess's OTLP
  **in-process**, renames those attrs to the semantic-convention names Langfuse
  maps, promotes `user.id` from the OWUI identity (so Langfuse's Users tab shows
  the real person, not Claude Code's subscription-account hash), and forwards to
  `HUBZOID_OTEL_ENDPOINT`. **No separate collector process** — it runs inside the
  bridge you already start, on its existing loopback port, and does nothing until
  you set the flag. It only acts on claude-local; the LiteLLM path is unaffected.

With `HUBZOID_OTEL_NORMALIZE=1` on claude-local (or nothing extra on the LiteLLM
path), Langfuse shows per-user / per-hub / per-model **cost** dashboards, with
users and sessions as first-class objects. Leave the flag off and you still get
the traces and attribution, just no cost on claude-local.

> **Pricing note:** Langfuse prices tokens from its own model table. If your
> hub pins a model Langfuse doesn't yet price (e.g. a very new Opus id), tokens
> map but cost stays 0 until you add that model's price once in Langfuse's
> settings. Check the model id on a span if cost is unexpectedly 0.

### Alternative: a shared collector

If you already run an OpenTelemetry Collector, or want a single normalization
point in front of *many* hubs and backends, do the same rename there instead of
per-bridge. A ready reference config is in
[`otel-collector.yaml`](./otel-collector.yaml): point `HUBZOID_OTEL_ENDPOINT` at
the collector (leave `HUBZOID_OTEL_NORMALIZE` **off** so you don't normalize
twice), and the collector exports to Langfuse. It is stock `otelcol-contrib` + a
config file, and is a no-op on the LiteLLM path. For a single hub, the in-bridge
flag is simpler — this is for fleets that already have collector infrastructure.

## Transport and exposure

Emission is **outbound push** over OTLP/HTTP (`http/protobuf`). Nothing is
exposed on the public Open WebUI port. Keep the OTLP receiver and the Langfuse
UI on a private network or loopback.

## Multi-hub gateway

Each hub runs its own bridge process, so set `HUBZOID_OTEL_ENDPOINT` per hub
(all pointing at the same central Langfuse). Turns separate cleanly by
`hubzoid.hub`, so one Langfuse view covers every hub.

## Content privacy

Prompt and response **bodies are not sent** by default (Claude Code redacts
them). Only enable `OTEL_LOG_USER_PROMPTS=1` / `OTEL_LOG_ASSISTANT_RESPONSES=1`
if you deliberately want message content in traces, and only where data
governance allows it.

## Data governance (per customer)

Whether a customer's production telemetry may flow to a shared Langfuse is a
per-customer decision, defaulted off. For egress-restricted customers, run
Langfuse inside their perimeter or leave OTel off. The box already calls the
model provider; adding an observability backend is a separate data-processing
choice.
