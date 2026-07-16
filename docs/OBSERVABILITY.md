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

## What you get

- **claude-local**: `claude_code.token.usage`, `claude_code.cost.usage` (USD),
  per-request `api_request` events, and (beta) traces, each tagged with
  `hubzoid.user`, `hubzoid.hub`, `hubzoid.surface` for **per-user, per-hub**
  cost. The user is the Open WebUI identity for that turn, also mirrored into
  the standard `user.id` resource attribute so backends' native user views
  (e.g. Langfuse's Users tab) pick it up.
- **OpenAI path**: LiteLLM's native `otel` callback emits tokens and
  `gen_ai.cost.total_cost`, tagged with `hubzoid.hub`. Per-user attribution on
  this path is a follow-up.

In Langfuse this lands as per-user / per-hub / per-model cost dashboards, with
users and sessions as first-class objects.

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
