# Hubs as their own services, and an audit log for security teams

Plan, 20 November 2026, based on the 2.0.9 release branch (`feat/mcp-parity`).
Status: built for 2.0.9 (branch `feat/scaling-audit`), decisions recorded on
20 November 2026. Reference designs read: Mastra (workers, PubSub,
`SensitiveDataFilter`, OTel exporter), Agno (DB-claimed schedules, Redis event
streams, OpenInference tracing, `@approval(type="audit")`), eve (`tracePolicy`)
and Open WebUI (`AUDIT_LOG_LEVEL`, `ENABLE_OTEL_*`).

## Problem

A company hosting Hubzoid on ECS with one RDS database and EFS, and a security
team watching it, hit these gaps:

- Every hub runs under the one `hubzoid run` supervisor on loopback ports. A
  hub cannot run as its own service (its own task size, image, secrets and
  deploys) behind the one web app. The old `--no-bridges` mode is gone.
- Only restricted tools leave an audit row. Other tool calls, sign-in failures,
  lockouts and API key use reach no log a SIEM reads, and log lines are plain
  text without the person or chat they belong to.
- `/healthz` says nothing about the database, there are no pool settings for a
  managed database, a hosted service can silently make its own deployment key,
  and nothing warns about SQLite on EFS.

## What was built

| Item | What |
|---|---|
| Per-hub address (C2) | `HUBZOID_BRIDGE_UPSTREAM` and `HUBZOID_INBOUND_UPSTREAM` in a hub's `.env`. The edge routes there and the supervisor only checks health (an "external" unit). `hubzoid run <hub> --behind <url>` runs the hub's own service. |
| Forwarded addresses (E3) | `HUBZOID_BRIDGE_HOST` and `--forwarded-allow-ips` from `HUBZOID_TRUSTED_PROXIES`, required on a network bind. |
| Tool-call audit (A1) | One emitter at the shared chokepoints (`run_checked`, `checked_server`, Claude's hooks). `HUBZOID_AUDIT=off|log|db|both`, `HUBZOID_AUDIT_ARGS=names|redacted`, table `hz_audit_events`, 90-day retention. |
| JSON logs (A2) | `HUBZOID_LOG_FORMAT=json` with request context, through the credential scrub. |
| Admin and sign-in events (A3, A4) | From the shared admin audit writers and the sign-in rate limiter. |
| Export (A5) | `GET /portal/api/audit/export` and `hubzoid audit --export`, JSON lines. |
| Activity page | Tool calls and Sign-in events tabs, filters, Export. |
| Readiness (D2) | `/readyz` on the bridge and the edge. |
| Hosted settings | `HUBZOID_REQUIRE_SECRET_KEY` (D4), database pool settings (C6), doctor check for SQLite on a network filesystem (C3), telemetry signals and content ceiling and `execute_tool` spans (B1). |

## Decisions (founder, 20 November 2026)

- One copy per hub for now. Running several copies of one hub (a per-copy
  supervisor folder, live chat events across copies through Postgres
  `LISTEN/NOTIFY`, database-claimed inbound dedup) waits for demand and goes
  to a GitHub discussion.
- Audit is off by default. Arguments keep names and identifiers by default.
- Not built: password sign-in warning beside OIDC (D1) and a pinned image
  user id (D3).

## Compatibility

Every new path is opt-in. With none of the new settings, routes, processes,
logs and telemetry are as in 2.0.8. One additive migration (`op_0090`).
