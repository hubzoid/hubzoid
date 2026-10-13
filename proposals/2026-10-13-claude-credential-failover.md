# Claude credential failover

Status: implementation authorized in chat on 13 October 2026; targets 1.3.2.
Issue: https://github.com/hubzoid/hubzoid/issues/9

## Problem

Issue #9 reports that Hubs using one `CLAUDE_CODE_OAUTH_TOKEN` stop answering
when that credential is limited, expired or revoked. Today an operator changes
the token and restarts. The existing plain error message explains the failure
but does not recover from it. No live failure was reproduced during this review.

## Why now

After releasing 1.3.1, the owner requested this issue next, with failover only
on a real break. On 13 October 2026 they confirmed the scope: the same Claude
model and runtime, with one primary credential and up to two backup credentials.

## What should happen

Confirmed intent: use a configured backup when the active credential is genuinely
unavailable, without changing the assistant's model, tools, permissions or Hub.
One credential must retain today's behavior.

The owner approved basic failover with minimal interruption and predictable
selection, explicitly removed alerts and admin UI from scope, and requested
fixing the reported Windows startup/cleanup issue in the same 1.3.2 release.
This direct implementation instruction supersedes the separate proposal PR and
plan approval gates. No publication is authorized by this document.

The agreed implementation behavior:

1. Optional `CLAUDE_CODE_OAUTH_TOKEN_SECONDARY` and
   `CLAUDE_CODE_OAUTH_TOKEN_TERTIARY` follow the existing primary in that order.
   Require an explicit primary when backups are configured. Ignore blank slots
   and deduplicate identical values. Never discover additional accounts from
   the host's login files or change the host's login.
2. Switch only on a terminal, structured Claude model-authentication failure
   or rate/usage-limit failure. A generic 403, billing problem, model refusal,
   invalid input, context overflow, MCP/tool error, process crash, timeout,
   cancellation or provider-wide 5xx/529 must not rotate credentials. Repeated
   unknown failures do not become evidence of a bad credential.
3. Retry the current request with a backup only if the SDK provides sufficient
   evidence that no model content or tool activity has begun. Close the old
   iterator/process and clean up its private MCP file before the next attempt.
   Once content, reasoning or tool activity starts, do not replay that turn.
   Remember a confirmed credential failure for the next independent request.
   If the SDK cannot establish a safe retry boundary, use next-request failover
   only; silence from the SDK is not proof that no action happened.
4. Each distinct credential gets at most one Hubzoid attempt per request:
   primary, secondary, tertiary. Preserve existing deadlines and cancellation.
   SDK-internal retries must be measured and documented separately; the three
   attempts are not a promise of three HTTP requests. Do not restart a workflow
   or resend an email, post, approval or other completed action.
5. Skip a limited credential until its reliable reset time. With no reliable
   reset, use a bounded cooldown; the value is five minutes.
   At expiry, allow one coordinated recovery probe on a real request while other
   requests continue on the working backup. Prefer primary again only after a
   successful probe. Invalid/revoked credentials stay disabled until operator
   replacement; restarting with the same token preserves the disabled state.
6. When all usable credentials fail, keep the existing plain error experience.
   Log slot labels, classified reasons and recovery times, never token values.
   Record successful recovery without leaving the request marked failed by an
   intermediate attempt. Keep incurred usage visible.
7. Use per-attempt SDK environment overrides, never mutate `os.environ` during
   a request. Blank inherited backup-token variables in children: the SDK
   merges the parent environment, so merely omitting those keys is insufficient.
   Preserve connector credentials, caller identity and tool access unchanged.

## Scope and non-goals

Cover the Claude runtime used by chat, MCP agent execution, scheduled agent
runs and eval subjects, plus the separate tool-free `claude_complete` path
used by `hub.call_llm`. Audit other direct SDK callers explicitly before
claiming complete coverage. Other runtime adapters and Hub file formats stay
unchanged. No new model router, proxy, dependency, token management UI, automatic
account provisioning, provider switching or parallel speculative requests.

Use the existing Claude SDK for execution. Own only the bounded selection and
failure-state policy that the SDK's model fallback does not supply. This does
not establish independent quota for different tokens or permission to share
subscription credentials across a team.

## Comparison and code evidence

Inspected the supplied `HubzoidAgent/RawTools` snapshots, not installed or run:
Agno package version **3.0.10**, Mastra core package version **1.68.0**. These
are local snapshot version fields, not claims about the latest published release.

| Project | Actual implementation inspected | Implication for Hubzoid |
|---|---|---|
| Agno | `agno-main/libs/agno/agno/models/fallback.py:76` selects error-specific lists; generic fallback excludes most 4xx, including 401/403. Its call wrappers catch `ModelProviderError`, not arbitrary application exceptions. | Reuse the distinction between provider failure and application failure. Credential-auth recovery needs an explicit policy beyond Agno's general fallback. |
| Agno streaming | `fallback.py:214` onward can activate a fallback after chunks have already been yielded. Its model response methods also own tool execution (`models/base.py`). | Do not copy a retry around the entire Claude agent run; already emitted content and executed actions cannot simply be undone. |
| Mastra | `mastra-main/packages/core/src/agent/__tests__/credential-error-fallback.test.ts:94` explicitly covers 401, 403, 429 and 500 fallback. `loop/workflows/agentic-execution/llm-execution-step.ts:1138` iterates model configurations; intentional processor aborts and total-run timeouts bypass fallback. Cancellation is handled separately. | Auth failure can justify trying separately configured credentials, but Hubzoid should use a narrower classification than this general model fallback. |
| Mastra continuation | `llm-execution-step.test.ts:1826` tests preserving the successful fallback index when continuing after a tool call. | Preserve completed work; do not restart a successful tool step. Hubzoid does not own the Claude SDK's internal model loop, so its safe boundary differs. |

Neither inspected fallback loop implements the issue's Claude OAuth credential
pool with quota-reset recovery. Adding either framework would not supply this
behavior directly and would add a second execution layer.

Official documentation checked on 13 October 2026:

- [Agno fallback models](https://docs.agno.com/models/fallback-models): ordered
  fallbacks, error-specific lists, per-model retries and streaming limitations.
- [Mastra model fallbacks](https://mastra.ai/models#model-fallbacks): ordered
  models with per-model retries and settings.
- [Claude Python Agent SDK](https://code.claude.com/docs/en/agent-sdk/python):
  structured `AssistantMessage.error`, per-query `env`, and `fallback_model`.
  The latter changes models; it is not a credential-pool setting.
- [Claude Agent SDK authentication guidance](https://code.claude.com/docs/en/agent-sdk/overview):
  Anthropic states that offering claude.ai login or rate limits in third-party
  products requires prior approval. Do not promote this as an approved shared
  subscription hosting path without establishing that deployment's eligibility.

Hubzoid code findings:

- `agent_errors.classify` intentionally uses broad text matches, including
  standalone 401/429 and stderr. Keep it for presentation; do not use it alone
  to authorize credential rotation.
- `ClaudeRuntime.stream_events` currently makes one SDK query and handles final
  errors. Structured assistant errors are available in the supported SDK.
- `claude_complete` makes a separate SDK query and currently loses detailed
  result errors. It needs the same explicit classification seam.
- `_options_for_turn`, `PrivateMcpConfig` and `_child_env` already provide the
  right integration points for cloned options and secret handling.

## Decisions and implementation

- Keep cooldowns and recovery claims in the existing shared operational
  `hz_meta` table, keyed by a SHA-256 token digest. Compare-and-swap updates
  coordinate bridges and prevent a late success clearing a newer failure.
  No migration, new dependency, background timer or external service.
- Use the provider reset when available; otherwise five minutes. A five-minute
  recovery lease bounds a crashed probe. A normal successful model response
  makes the credential eligible again; unrelated failures do not mark it healthy.
- The original issue's broad consecutive-failure trigger is excluded. No admin
  alerts or remaining-quota UI, as explicitly requested. Runtime limit events
  inform reset timing, but warning/allowed events do not initiate switching.
- Failover uses the existing Claude adapter in chat and tool-free completion,
  including workflow calls and the eval judge. SDK defaults and other backends
  remain unchanged. Ambiguous mixed OAuth/API/provider authentication is refused
  only when backups are configured.
- Windows defects reproduced by code inspection and focused regression seams:
  migrations and workflow ownership imported `fcntl`, and cleanup accessed
  `SIGKILL`. One stdlib lock helper uses POSIX flock or Windows msvcrt byte locks;
  related schedule, eval, inbound, Codex refresh and GitZoid checkout callers
  share it. Windows process trees use the system taskkill executable. PostgreSQL
  ownership and POSIX process-group behavior are unchanged.

## Definition of done

Focused fake-SDK/clock tests prove single-credential compatibility, ordered
two-backup recovery, exhausted pools, no rotation on unrelated failures, reset
recovery, concurrent probes, token replacement, cancellation, iterator cleanup,
and no replay after partial output or tool execution. Include structured
assistant errors and failed result messages, successful-fallback accounting,
tool-free calls, masked child environments and secret-free logs.

Run a small real-SDK smoke check with non-mutating tools to verify the adapter
and environment boundary. Do not intentionally exhaust a live subscription.
Record untested live quota/reset behavior rather than treating mocks as provider
acceptance. Run focused tests serially; no full suite or heavy local build.

Document configuration in `hubzoid/settings.py` and the runtime guide, including
cooldown, attempts, permission boundaries, absence of alerts, restart behavior and
the fact that multiple tokens do not guarantee independent quotas. Do not claim
native Windows acceptance from Mac tests; run the focused Windows CI job.


## Implementation validation

- Claude adapter regression: 423 passed, including PostgreSQL recovery claims.
- Lifecycle, migrations, inbound and eval checks on macOS: 88 passed, one native
  Windows-only acceptance skipped locally and exercised on the Windows runner.
- Schedule, eval persistence, Codex and GitZoid regression: 217 passed; two live
  Codex tool-isolation cases excluded from this focused local run.
- Final failover/secret/error checks after tightening terminal-result handling:
  31 passed; the PostgreSQL case was deselected in this final small rerun and
  had already passed in the broader adapter run.
- Native Windows CI run 38350734884 passed the headless startup, shutdown,
  cross-process SQLite locks, migration and credential-pool checks.
- Release-workflow regression: 13 passed after adding the Windows workflow to
  the explicit inventory. No full local suite or build was run.
- Live quota exhaustion/reset remains untested. No real-SDK provider smoke was
  run for this change; mocked SDK messages do not prove provider acceptance.
