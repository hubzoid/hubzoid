# Observability

Hubzoid can send OpenTelemetry traces of every agent turn to any OTLP/HTTP
backend, such as Langfuse. Use it to see latency, tokens and cost per person,
per agent and per model. It is off by default. Claude Code and LiteLLM emit
the telemetry. Hubzoid sets the destination and tags each turn with who asked,
on which agent and surface.

```bash
# in the hub's .env, or the process environment (systemd, Docker)
HUBZOID_OTEL_ENDPOINT=https://langfuse.internal/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public_key:secret_key)>
HUBZOID_OTEL_NORMALIZE=1        # claude-local only: show cost in Langfuse
```

Restart the hub. Every turn now emits trace spans
(`interaction -> llm_request -> tool`). Leave `HUBZOID_OTEL_ENDPOINT` unset and
nothing is emitted.

For the security view (every tool call, sign-in and admin change as a log
line), see the [audit log](AUDIT.md).

## Settings

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_OTEL_ENDPOINT` | unset (off) | The OTLP/HTTP endpoint. Setting it turns telemetry on. |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | Standard OTel headers, passed straight to the exporter. Langfuse uses HTTP Basic with its public and secret keys. |
| `HUBZOID_OTEL_NORMALIZE` | off | claude-local only. Renames token attributes so Langfuse can price them, and tags traces with the hub. See [Show cost on claude-local](#show-cost-on-claude-local). |
| `HUBZOID_OTEL_SIGNALS` | `traces` | What the agent runtime exports: any of `traces`, `metrics`, `logs`. An operator's own `OTEL_METRICS_EXPORTER` or `OTEL_LOGS_EXPORTER` wins. |
| `HUBZOID_OTEL_CONTENT` | unset | The most prompt and tool content telemetry may carry: `none`, `tools` or `full`. See [Control what content is sent](#control-what-content-is-sent). |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | OTel defaults | Standard trace sampling. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | unset | Only for pushing eval runs. Read from `OTEL_EXPORTER_OTLP_HEADERS` when that holds Basic auth. |

Only traces are emitted by default. Tokens and cost ride the trace spans, a
trace backend like Langfuse drops metrics and logs, and logs can carry prompt
content.

## What each trace carries

Every span has these resource attributes, so per-hub and per-surface filters
work on any OTLP backend:

| Attribute | Value |
|---|---|
| `hubzoid.user` | The signed-in person |
| `hubzoid.hub` | The hub |
| `hubzoid.surface` | Where the turn came from (web, Slack, MCP and so on) |
| `user.id` | The same person, for backends with a Users view |

Whether cost shows depends on the model path:

| Model path | Token and cost attributes | Cost in Langfuse |
|---|---|---|
| OpenAI and LiteLLM (OpenRouter, Azure, OpenAI and others) | Standard `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.cost.total_cost` | Works directly, no collector |
| claude-local (Claude Code) | Non-standard `input_tokens`, `output_tokens`, `cache_*_tokens` | Shows 0 unless `HUBZOID_OTEL_NORMALIZE=1` is set |

## Send traces to Langfuse

Point `HUBZOID_OTEL_ENDPOINT` at Langfuse's OTel path (`.../api/public/otel`)
and set the Basic auth header, as in the example at the top.

When the endpoint is Langfuse (its path ends in `/api/public/otel`), Hubzoid
also sends `x-langfuse-ingestion-version: 4` by itself on every export path:
the claude-local subprocess, the `HUBZOID_OTEL_NORMALIZE` relay and the
LiteLLM path. Langfuse v4 asks OTLP senders for this header and may delay or
stop uploads without it. You do not need to add it to
`OTEL_EXPORTER_OTLP_HEADERS`. A value you set there yourself wins, and the
header is never sent twice. Other OTLP endpoints get no extra header.

Exporters that Hubzoid does not start are yours to configure. If you run
`opentelemetry-instrument hubzoid run` or a collector that sends straight to
Langfuse, add `x-langfuse-ingestion-version=4` to its headers yourself.

Langfuse prices tokens from its own model table. If a hub pins a model
Langfuse does not price yet (for example a very new model id), tokens map but
cost stays 0 until you add that model's price once in Langfuse's settings.

## Show cost on claude-local

```bash
HUBZOID_OTEL_NORMALIZE=1
```

With this set, the bridge intercepts its own `claude` subprocess's OTLP in
process. It renames the token attributes to the semantic-convention names
Langfuse maps, sets `user.id` from the signed-in identity (so Langfuse's Users
tab shows the real person, not Claude Code's subscription account), and
forwards to `HUBZOID_OTEL_ENDPOINT`. It runs inside the bridge on its existing
loopback port, so there is no separate collector process. It acts only on
claude-local. The LiteLLM path is unaffected.

It also sends the hub name as a Langfuse tag (`langfuse.trace.tags`). Langfuse
cannot group dashboards by a custom metadata field like `hubzoid.hub`, but it
can group by tags, so this makes a "cost by hub" chart possible, and lets you
cross hub and user. Without the flag you still get traces and attribution,
with no cost on claude-local.

## Use a shared collector

If you already run an OpenTelemetry Collector, or want one normalization
point in front of many hubs and backends, do the same rename there. A
reference config is in [`otel-collector.yaml`](./otel-collector.yaml). It is
stock `otelcol-contrib` with a config file.

1. Point `HUBZOID_OTEL_ENDPOINT` at the collector.
2. Leave `HUBZOID_OTEL_NORMALIZE` off, so traces are not normalized twice.
3. The collector exports to Langfuse. Its Langfuse exporter sets
   `x-langfuse-ingestion-version: "4"` itself, since Hubzoid adds that header
   only when it talks to Langfuse directly.

The collector config does nothing on the LiteLLM path. For a single hub, the
in-bridge flag is simpler.

## Choose signals

`HUBZOID_OTEL_SIGNALS` chooses what the agent runtime exports, for example:

```bash
HUBZOID_OTEL_SIGNALS=traces,metrics
```

An operator's own `OTEL_METRICS_EXPORTER=otlp` or `OTEL_LOGS_EXPORTER=otlp`
still wins.

## Trace Hubzoid itself

For spans from Hubzoid's own code (the web server, the database and one
`execute_tool` span per tool call), run it under the OpenTelemetry Python
auto-instrumentation:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install
opentelemetry-instrument hubzoid run
```

Each `execute_tool` span has `gen_ai.tool.name`, `gen_ai.tool.call.id`,
`error.type` and the hub, person and surface. Tool spans never carry
arguments or results.

On AWS, run the AWS Distro for OpenTelemetry collector as a sidecar and point
`HUBZOID_OTEL_ENDPOINT` and `OTEL_EXPORTER_OTLP_ENDPOINT` at it
([deploy/ecs-adot-collector.yaml](deploy/ecs-adot-collector.yaml)).

## Control what content is sent

Prompt and response bodies are not sent by default (Claude Code redacts them).
`HUBZOID_OTEL_CONTENT` sets the most content telemetry may carry, whatever
else is set:

| Value | Carries |
|---|---|
| `none` | Names, counts and timings |
| `tools` | Plus which tools and servers, with their parameters |
| `full` | Plus prompts, answers and tool results |

Unset leaves Claude Code's own `OTEL_LOG_*` switches as the environment has
them. Choose `full` only where your data governance allows it.

Whether production telemetry may go to a shared Langfuse is a decision for
each organization, and it is off by default. Where data must not leave a
network, run Langfuse inside it or leave OpenTelemetry off. The server already
calls the model provider. Adding an observability backend is a separate
data-processing choice.

## Push eval runs to Langfuse

If the hub has `evals/*.md` cases (see [evals.md](evals.md)), each run is also
pushed to Langfuse as traces with scores: one trace per case, one score per
assertion, plus the judge's score and reasoning. This needs the Langfuse API
keys alongside the endpoint:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

If `OTEL_EXPORTER_OTLP_HEADERS` already holds Basic auth, the keys are read
from there instead.

Filter eval traffic out of your production dashboards. Every eval trace is
tagged `hubzoid.eval` and carries `hubzoid.eval: true` in its metadata. Eval
traffic is synthetic and would skew latency and cost numbers.

The push is best-effort and optional. Evals work with no endpoint at all: the
terminal table, the run JSON under `<hub>/.hubzoid/evals/` and
`hubzoid eval status` need no infrastructure.

## Several hubs

Each hub runs its own bridge process, so set `HUBZOID_OTEL_ENDPOINT` per hub,
all pointing at the same Langfuse. Turns separate by `hubzoid.hub`, so one
Langfuse view covers every hub.

## Network exposure

Telemetry is an outbound push over OTLP/HTTP (`http/protobuf`). Nothing is
exposed on the public port. Keep the OTLP receiver and the Langfuse UI on a
private network or loopback.

## Troubleshooting

| Symptom | Check |
|---|---|
| No traces at all | `HUBZOID_OTEL_ENDPOINT` is set in the hub's environment, and the hub was restarted. |
| Traces arrive, cost is 0 on claude-local | Set `HUBZOID_OTEL_NORMALIZE=1`, or normalize in a collector. |
| Cost is 0 on one model only | Langfuse has no price for that model id. Add it in Langfuse's settings. |
| Langfuse delays or drops uploads from a collector or `opentelemetry-instrument` | Add `x-langfuse-ingestion-version=4` to that exporter's headers. |
| No metrics or logs | Only traces are on by default. Set `HUBZOID_OTEL_SIGNALS`. |
| Eval traces mixed into production charts | Filter out the `hubzoid.eval` tag. |
