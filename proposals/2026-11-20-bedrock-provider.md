# Amazon Bedrock as a model provider

Plan, 20 November 2026. Based on SDK 2.0.8 (`v2.0.8`, local `main`, the files below match `origin/main`), read but not run. CLI behaviour is from the Claude Code docs as published on 20 November 2026 ([Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock), [environment variables](https://code.claude.com/docs/en/env-vars), [prompt caching](https://code.claude.com/docs/en/prompt-caching)). LiteLLM behaviour is from the 1.95.1 source we pin. IAM resource types are from the [AWS service authorization reference for Bedrock](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrock.html). Reviewed once by Codex (gpt-6.1-sol, high). Status: built for 2.0.9 (branch `feat/bedrock-s1`), decisions recorded on 20 November 2026. See "Build notes" at the end for what the build settled.

## Problem

A company that already pays for AWS, or must keep model traffic in its own AWS account, cannot run a Hubzoid hub on Bedrock in a way we stand behind. Today it would have to set `CLAUDE_CODE_USE_BEDROCK` and the model pins by hand in `.env`, keep `MODEL=claude-local`, and live with these gaps:

- `_validate_model_pin` (`factory_claude.py:759`) replaces any pin that is not a tier or `claude-*` with `sonnet`, so a Bedrock id such as `us.anthropic.claude-sonnet-4-6` or an inference profile ARN never reaches the CLI.
- `_child_env` (`factory_claude.py:259`) treats the hub as a subscription hub: it probes `claude auth status`, and the billing log says "Using the Claude subscription".
- `doctor` (`doctor.py:486`) fails the model check without a Claude login, and the Settings diagnostics say "Claude Code isn't signed in".
- Without explicit pins the CLI's Bedrock defaults apply: the primary model is Opus 5.5 and the `sonnet` alias is Sonnet 4.5. A hub that asked for `claude-local` (Sonnet) is billed at Opus rates.
- The native web search switch shows, but Bedrock has no WebSearch tool.
- The LiteLLM route (`MODEL=bedrock/<id>`, which `docs/providers.md` mentions in one line) adds `cache_control` to every Bedrock model, including families that do not cache, and nothing checks AWS credentials or the region.

## Why now

The founder asked for Bedrock as an opt-in on 20 November 2026, split into S1 (models through Bedrock, this plan) and S2 (AgentCore hosting, separate). Customer-controlled hosting is a framework requirement, and model access through the customer's own cloud account is part of that for AWS customers.

## What should happen

### One model name, opt-in

A hub opts in with one line in its `.env` (or `model:` in `AGENTS.md`):

| `MODEL=` | Engine | What Hubzoid sends |
|---|---|---|
| `bedrock/sonnet`, `bedrock/opus`, `bedrock/haiku` | Claude Agent SDK | The tier alias, with the tiers pinned to Bedrock ids |
| `bedrock/<Bedrock Claude id>`, for example `bedrock/us.anthropic.claude-sonnet-4-6`, `bedrock/global.anthropic.claude-opus-4-8`, `bedrock/eu.anthropic.claude-sonnet-4-6[1m]` | Claude Agent SDK | That id as the primary model |
| `bedrock/arn:aws:bedrock:…:foundation-model/<id>` or `…:inference-profile/<id>` | Decided from the id inside the ARN | That ARN |
| `bedrock/arn:aws:bedrock:…:application-inference-profile/<id>` | Decided from the profile's backing model (below) | That ARN |
| `bedrock/<any other id>`, for example `bedrock/amazon.nova-pro-v1:0` | OpenAI Agents SDK through LiteLLM | `bedrock/<id>` unchanged |
| `bedrock/converse/<id>`, `bedrock/invoke/<id>` | OpenAI Agents SDK through LiteLLM | Unchanged. This is the way to keep a Claude model on LiteLLM on purpose |

`MODEL=claude-local` with the subscription token stays the default and does not change in any way. Nothing about a hub that does not say `bedrock/` changes.

A Claude id is a tier alias or one that matches `[<prefix>.]anthropic.claude-…` (prefix `us`, `us-gov`, `eu`, `apac`, `jp`, `au` or `global`, optional `:N` version and `[1m]` suffix). An application inference profile ARN does not show its model, so Hubzoid resolves it once per process with `bedrock:GetInferenceProfile` when the runtime is built, and routes it by the backing model. If the lookup is denied or fails, the hub does not start and the message says to grant `GetInferenceProfile` or to write `bedrock/converse/<arn>` for LiteLLM. Hubzoid never guesses the engine for an opaque ARN.

### One resolver for the effective model

Today the hub's model and every override are interpreted in many places, each with its own `startswith("claude-local")` test: `runtime.build`, `describe`, `complete_once`, `run_station`, `_apply_model_override`, `handover.engine` and `classify`, `evals/judge.py` (`resolve_model` and the call), `stations/definitions._build`, `stations/cli.py`, `budgets.priced`, `doctor._model`, `admin/diagnostics.check_model_auth`, `hubs/get_started.py`, `cli._check_local_reply`, `_child_env`, the chat catalog selection (`chat/engine_routes.py:203`) and the per-turn budget admission in `ClaudeRuntime.stream_events` (`factory_claude.py:1297`, which passes the literal `"claude-local"`).

They all move to one resolver in `runtime.py`: `effective_model(hub_dir, override=None)` returns the model string, its **engine** (`test`, `codex`, `claude`, `litellm`) and its **provider** (`subscription`, `bedrock`, `api`). It reads the hub's model the same way `_resolve_model_id` does (`.env`, then `AGENTS.md`), never `os.environ["MODEL"]` alone, which today misses a hub that sets its model in `AGENTS.md`. Every entry point that runs a model or reports on one calls it: chat, `hub.call_llm` (`complete_once`), stations, the eval judge, scheduled runs, delegates, the reply check, diagnostics, onboarding and station inspection. A new small module `hubzoid/bedrock.py` holds the id rules, the tier pins, region resolution and the child environment. Runtime-specific construction stays in the runtime files, as AGENTS.md requires.

**Bare names follow the hub, explicit ones are used as written.** On a Bedrock hub, an override of `opus`, `sonnet`, `haiku`, a `claude-*` id or a Bedrock Claude id resolves to `bedrock/<x>`. An id that names its backend is used as written, as today: `claude-local/<x>` (or a station's `engine: claude-local`) runs on the subscription, `openai/…` and `bedrock/converse/…` run on LiteLLM. This holds for a scheduled task's `model:`, `hub.call_llm(model=...)`, a judge model, a station's `model:` and a sub-agent's `model:`. So a Bedrock hub can send chosen tasks to the subscription, and a `claude-local` hub can send chosen tasks to Bedrock with `bedrock/<x>`.

Each Claude call already builds its own child environment (`_child_env(model)` per runtime, `tool_free_options`, `claude_station`), so a mixed hub keeps the credentials apart per call: a Bedrock call gets AWS credentials and no OAuth token, a `claude-local` call gets the OAuth token and no AWS credentials, exactly as today. A `claude-local/<x>` call on a Bedrock hub needs `CLAUDE_CODE_OAUTH_TOKEN` in that hub's `.env`. Without it the call fails with a clear message. It never falls back to Bedrock, and a Bedrock call never falls back to the subscription. Usage rows record the provider that served each call. On a `claude-local` hub with no `bedrock/` overrides nothing changes.

**Delegates compare engine and provider.** `handover.classify` makes a native delegate only when both engine and provider match the hub (after the mapping above). A native delegate runs inside the parent's CLI process and shares its credentials, so a sub-agent on the other provider (`claude-local/haiku` on a Bedrock hub, `bedrock/haiku` on a subscription hub) stays an inline skill, as cross-provider declarations are today. Without this, sharing one engine label would run such a sub-agent under the parent's provider.

**Chat catalog choices.** A catalog entry (`chat.models.catalog`) is checked against the hub when the catalog loads and when a turn names it. On the Claude runtime an accepted entry is passed to the SDK as the CLI pin (`bedrock/opus` becomes `opus`, `bedrock/<id>` becomes `<id>`), never with Hubzoid's prefix. An entry that needs another engine or provider than the hub's is refused with a clear message: per-turn switching between runtimes is not part of this plan. This covers single selection, comparison and synthesis.

### Claude models: the Claude Agent SDK runtime

`build_claude_runtime`, `claude_complete`, `claude_station` and `tool_free_options` already take the model setting. For a Bedrock model, `_child_env` returns, on top of today's overrides:

| Variable | Value | Why |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | `1` | The CLI calls Bedrock |
| `AWS_REGION` | The resolved region (below) | Both engines use the same region, set explicitly |
| `ANTHROPIC_MODEL` | The resolved primary id | Primary and background tasks use the hub's model, never the CLI's Opus 5.5 default |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`, `_SONNET_`, `_HAIKU_` | The tier pins (below) | Delegates and overrides that say `opus`, `sonnet` or `haiku` stay on Bedrock, on the version we chose |
| `CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK` | `1` | An unattended run fails loudly instead of switching to an older model when access is lost |
| `CLAUDE_CODE_OAUTH_TOKEN` (and the two backup slots), `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` | blank | Billing is unambiguous: AWS only |
| `CLAUDE_CODE_USE_VERTEX`, `_FOUNDRY`, `_MANTLE` | blank | One provider per hub |
| `CLAUDE_CODE_PROMPT_CACHE_TTL` | From `HUBZOID_CACHE_TTL` (default `1h`) | Same cache knob as the LiteLLM path |
| `DISABLE_PROMPT_CACHING` | `1` when `HUBZOID_DISABLE_PROMPT_CACHE` is on | Same off switch as the LiteLLM path |

The `model` option passed to the SDK is the tier alias or the id itself. The CLI resolves aliases through the pins, and Anthropic-format ids (`claude-opus-4-8`) through its own Bedrock mapping.

**Tier pins.** The CLI's own defaults lag (the `sonnet` alias is Sonnet 4.5 on Bedrock). Hubzoid pins each tier to the version that tier means on `claude-local` at release time, written as `<prefix>.anthropic.<model>`. The prefix follows the CLI's rule (`us-gov-*` to `us-gov`, `us-*` to `us`, `eu-*` to `eu`, `ap-*` to `apac`, otherwise `global`), or `ANTHROPIC_BEDROCK_REGION_PREFIX` when set. An operator overrides any tier by setting the CLI's own `ANTHROPIC_DEFAULT_<TIER>_MODEL` in `.env`, which Hubzoid passes through untouched. The table lives in one place, `hubzoid/bedrock.py`, and is checked against the AWS model catalog when built.

**Settings files are not read.** Hubzoid runs the CLI with `setting_sources=[]`, so what `/setup-bedrock` writes to `~/.claude/settings.json` does not apply to hubs. Configuration is `.env` only. The docs say so, and the proof checks it.

**Credentials.** The CLI uses the standard AWS chain: instance or task role, `AWS_PROFILE`, access keys, or `AWS_BEARER_TOKEN_BEDROCK` (which the CLI prefers when set).

`config_secrets.child_env_overrides` keeps AWS credentials only when `CLAUDE_CODE_USE_BEDROCK` is in the parent environment. When Hubzoid sets it only for the child, the parent does not have it, so today's code would blank the very credentials the CLI needs. The function gets a `bedrock` argument, and `_child_env` passes `True` for a Bedrock model. A `claude-local` hub still blanks AWS credentials exactly as today.

**Subscription probe and notes.** On a Bedrock model, `_child_env` does not call `claude_login()` and does not log "Using the Claude subscription". `ignored_key_note` gets a Bedrock form: "On Bedrock, CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY in the environment are ignored." Doctor, the Settings diagnostics, onboarding and `hubzoid stations` skip the Claude login for Bedrock models and run the AWS check instead.

**OAuth failover.** `claude_credentials.Pool` already refuses OAuth failover when `CLAUDE_CODE_USE_BEDROCK` is set by hand, and that stays. For `MODEL=bedrock/...`, the Pool is not used at all. `_query_with_failover` receives the effective model of the call (not the hub's base setting), and `Pool.configured` returns `None` for a Bedrock provider, so no OAuth token is injected into the final options (today it injects one at `factory_claude.py:1081`). This matters on a shared box: a deployment-level `CLAUDE_CODE_OAUTH_TOKEN` and backup tokens meant for the `claude-local` hubs must not stop a Bedrock hub from starting or reach its child. The provider page says failover is a subscription feature, and on Bedrock retries and quotas are AWS's.

**Pin validation.** `_validate_model_pin` accepts Bedrock Claude ids (with `[1m]`) and Bedrock ARNs only when the provider is Bedrock. On `claude-local` it behaves exactly as today. An unknown pin still falls back with a warning to `sonnet`, which on Bedrock is the pinned Sonnet.

**Stations.** A station with no `engine:` follows the hub, so on a Bedrock hub it runs on Bedrock (`engine: bedrock`, a Claude model only), and `run_station` uses `claude_station` with the Bedrock environment. A station that says `engine: claude-local` runs on the subscription, as an explicit id does. Station files without an engine stay portable between hubs.

**Web search.** Bedrock does not offer the WebSearch tool. On a Bedrock hub, `web_on()` in `chat/config_routes.py` returns false, so the switch does not show, and doctor warns when `retrieval.web.search` is `native`. A turn that still asks for it gets a notice instead of a CLI error.

**Service tier** passes through: `ANTHROPIC_BEDROCK_SERVICE_TIER` is kept for the child like other `ANTHROPIC_*` keys. Documented, not tested in this plan. Guardrails are out of scope (they also need `bedrock:ApplyGuardrail` on the guardrail).

### Other models: LiteLLM

The path exists today (`model.build` makes a `LitellmModel`, LiteLLM signs with boto3, which is already a core dependency). Changes:

- **Region.** LiteLLM reads the region from an ARN, then `AWS_REGION_NAME`, then `AWS_REGION`, then the profile, and otherwise uses `us-west-2` (`base_aws_llm.py:590`). The CLI otherwise uses `us-east-1`. So Hubzoid resolves one region for a Bedrock hub and passes it to LiteLLM as `aws_region_name`, as it sets `AWS_REGION` for the CLI.
- **Caching.** `caching._supports_cache_control` matches the word `bedrock`, so it marks every Bedrock model. For a Bedrock id it asks `litellm.utils.supports_prompt_caching` instead (true for Claude and Nova, false for Llama and DeepSeek in 1.95.1). LiteLLM turns `cache_control` into a Bedrock `cachePoint` and forwards the 1h TTL only for models whose price row has a 1h cache price. Other models get 5 minutes without an error. The provider page says so. Whether a cache mark on a non-caching family actually fails is reproduced in the build before it is called a bug fix.
- **Credentials.** `model.build` does not require a key for `bedrock/`, as today. Doctor runs the AWS check.
- **Tool calling** varies by model on Bedrock. The docs name the models we tested.

### Region

A Bedrock hub needs one known region. Hubzoid resolves it from `AWS_REGION`, then `AWS_DEFAULT_REGION`, then the active profile's region, or the region in an ARN model id. With none of these, the hub does not start and doctor fails, because the two engines would otherwise fall back to different regions (`us-east-1` and `us-west-2`), and the region is often the reason a company chose Bedrock. Doctor shows the region and where it came from.

### Doctor

Doctor's model check and the Settings diagnostics get one Bedrock check, used for both engines, on the effective model:

1. **Region**, as above. Fails when none resolves.
2. **Identity.** `sts:GetCallerIdentity` with the resolved region, showing the account and role. With `AWS_BEARER_TOKEN_BEDROCK` there is no STS identity, which the check says.
3. **Model.** The lookup matches the resource type, after removing CLI modifiers such as `[1m]`: inference profile ids and ARNs with `GetInferenceProfile`, application inference profiles with `GetInferenceProfile` (and their backing model must match the engine chosen), foundation model ids and ARNs with `GetFoundationModel`. A missing profile id says which prefixes the account does have (`ListInferenceProfiles`). The primary model must resolve. The three tier pins are listed for information, and only a pin the hub actually uses (in a task, sub-agent, station or catalog) is checked strictly. A denied lookup is reported as missing discovery permission, separately from inference access.
4. **Access.** Only a real call proves inference access, the use-case form and the Marketplace subscription. The existing active check (`check_model_reply`, "The model answered") is that proof once routing works. Its failure text for Bedrock points at the IAM policy and the Bedrock console.
5. **Ignored credentials and web search**, as above.

No new dependency: boto3 is already required.

### Cost and usage

- **One conversion for every Claude entry point.** Today chat (`_record_claude_usage`), `claude_complete` (`factory_claude.py:1596`) and `claude_station` (`factory_claude.py:1661`) each read the CLI's result in their own way. On Bedrock they share one function that records the CLI's `total_cost_usd` (the CLI's own figure, the same source a `claude-local` hub with an API key uses) and the effective Bedrock model as the row's model. Aggregate tokens are not repriced at one model's rate, because a turn can include background and delegate models, and the cache detail needed to split 5 minute and 1 hour writes is not available per model.
- **Unknown stays unknown.** When the CLI reports no cost on Bedrock, the row is recorded with no cost and `usage.record` does not replace it with a LiteLLM estimate (today it estimates every missing cost, `usage.py:101`).
- **Budgets.** `budgets.priced` treats a Bedrock Claude model as priced, like `claude-local`, because the CLI reports a cost. Per-turn admission uses the effective model, not the literal `"claude-local"`. Today admission subtracts only numeric costs (`budgets.py:325`), so a run of unpriced calls would never reach a dollar limit. On Bedrock, a dollar-limited scope that already holds an unpriced Bedrock row refuses further calls with a message naming it, as an unpriced model is refused today. Other Bedrock models are priced by LiteLLM, as today.
- **Known risk.** Bedrock charges a premium on regional profiles (`us.`, `eu.`, `apac.`) over `global.`, about 10 percent in LiteLLM's table. Whether the CLI's figure includes it is not documented. The proof compares one day of recorded cost with AWS Cost Explorer. If they differ, a correction is a follow-up with its own evidence.
- `claude-local` cost rows do not change.

### IAM policy

The provider page ships a policy in separate statements, because `bedrock:ListInferenceProfiles` has no resource type and needs `"Resource": "*"`:

- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the inference profiles, application inference profiles and foundation models used.
- `bedrock:GetInferenceProfile` and `bedrock:GetFoundationModel` on the same resources.
- `bedrock:ListInferenceProfiles` on `*`.
- The Marketplace subscription statement from the Claude Code docs, for first use.

`sts:GetCallerIdentity` needs no grant. The policy is validated with a restricted test role, not administrator credentials. The page recommends a role (instance or task) over keys, and an AWS account or application inference profile per deployment for cost tracking.

Credentials reach the CLI child, and the stdio MCP servers it starts inherit its environment, as with today's manual Bedrock setup. The page says this plainly and recommends a role or a Bedrock-only API key over account keys.

### Docs and starter

- New page `docs/bedrock.md`: when to choose it, the `MODEL` table, credentials, region and prefix, pins and overrides, IAM policy, caching, cost, web search, what failover means here, troubleshooting (on-demand throughput errors, region, model access), and an agent-readable setup prompt.
- `docs/providers.md`: Bedrock in the provider list, linking the page. The one line saying LiteLLM handles `bedrock/` is corrected.
- `docs/DEPLOYING.md` (lines 159 to 160): `MODEL=bedrock/...` replaces setting `CLAUDE_CODE_USE_BEDROCK` by hand.
- `cli.py` starter `.env`: a commented Bedrock stanza (`MODEL=bedrock/sonnet`, `AWS_REGION=`, a role or `AWS_PROFILE` note).
- `CHANGELOG.md`: the routing change for Claude ids under `bedrock/`.

### Dependency floor

The CLI features used need Claude Code 2.1.242 (`CLAUDE_CODE_PROMPT_CACHE_TTL`) and 2.1.285 (`CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK`). The pin `claude-agent-sdk>=0.2.159` may bundle an older CLI (0.2.182 bundles 2.1.325). The build raises the floor to the first release that bundles 2.1.285 or later, checked from the package, not assumed. Because this also moves `claude-local` hubs to a newer CLI, the subscription regression checks below run on the new floor.

## The trade-off

LiteLLM uses the same `bedrock/` prefix. After this change, a Claude id under `bedrock/` runs on the Claude Agent SDK instead of LiteLLM. Checked on 20 November 2026: no SDK code, test, sample hub or Isha hub uses `bedrock/` today, and the only mention is the one line in `docs/providers.md`. A hub that wants LiteLLM for a Claude model writes `bedrock/converse/<id>`. The changelog says so.

## Scope and non-goals

- Not S2: AgentCore or any hosting change.
- Not Mantle (`CLAUDE_CODE_USE_MANTLE`), Vertex or Foundry.
- Not `codex-local` on Bedrock.
- Not per-turn switching between runtimes from the chat catalog.
- Not a model picker or catalog listing from AWS.
- Not Bedrock guardrails.
- Not cache-aware cost for `claude-local` estimates. That path sums cache tokens into input and over-estimates. It is a separate follow-up.
- No change to `claude-local`, its child environment, its login, failover, delegates or costs.

## Tests

Focused unit tests, extending existing files where they exist:

- `effective_model` over every id shape and override, on a Bedrock hub and a `claude-local` hub, including a model set only in `AGENTS.md`.
- **Final options, not helpers.** For each Claude entry point (chat after per-turn cloning, `hub.call_llm`, stations, the eval judge, a scheduled run, the reply check), capture the options that reach `query()`, with primary and backup OAuth tokens and an API key in the parent, no parent `CLAUDE_CODE_USE_BEDROCK`, and telemetry on. On Bedrock: the Bedrock variables are set, AWS credentials kept, no OAuth token or key present, no failover injection. On `claude-local`: identical to today.
- Delegates in both directions: a bare `haiku` sub-agent on a Bedrock hub runs as a Bedrock delegate, a `claude-local/haiku` sub-agent there stays a skill, and a `bedrock/haiku` sub-agent on a `claude-local` hub stays a skill.
- Mixed hubs: on a Bedrock hub, a task with `model: claude-local/opus` reaches `query()` with the OAuth token and no AWS credentials or Bedrock variables, and fails clearly without a token. On a `claude-local` hub, a task with `model: bedrock/opus` reaches `query()` with the Bedrock variables and no OAuth token.
- Catalog entries: translation on the Claude runtime and refusal of a cross-engine entry, for selection and comparison.
- Pin validation scoped to Bedrock, cache gating, the shared usage conversion, unknown cost kept unknown, and repeated unpriced Bedrock calls against a dollar limit.
- ARN routing with a stubbed `GetInferenceProfile` (Claude backed, Nova backed, denied).
- Region resolution: conflicting selectors, profile only, none.
- Doctor with a stubbed boto3.
- Real-provider tests in `tests/e2e/`, skipped without AWS credentials.
- One broader regression run before handover, after the focused checks pass.

## Definition of done

An end-to-end run on HubzoidTestHub with a real AWS account that has Claude enabled in Bedrock, on both engines (Claude on the Claude Agent SDK, one named non-Claude model on LiteLLM), on a box in ap-south-1:

1. Chat with a tool call, streamed in the web app.
2. Caching: the second turn reports cache read tokens. On Claude, a 1h write shows as `ephemeral_1h_input_tokens`, `HUBZOID_CACHE_TTL=5m` gives 5 minute writes, and `HUBZOID_DISABLE_PROMPT_CACHE` gives none.
3. A cost row per turn, and one day's total compared with Cost Explorer for the account.
4. A scheduled run with `model: opus` that stays on the Bedrock Opus pin.
5. Proof that AWS served each call, not only the model name in the usage row: matching `InvokeModel` events in CloudTrail (or Bedrock invocation metrics in CloudWatch) for the test account at the run times.
6. `hubzoid restart`, then a chat that still works.
7. On the same box, a Bedrock hub and a `claude-local` hub running together under `hubzoid run`, with a deployment-level OAuth token and backup tokens set. The `claude-local` hub still answers on the subscription, its child has no `CLAUDE_CODE_USE_BEDROCK` and no AWS credentials. The Bedrock hub starts, its child has no OAuth token, and no subscription usage appears for its calls.
8. `hubzoid doctor` passes on the Bedrock hub, and fails with a useful message for a missing region, a model the account lacks, a denied lookup and a non-Anthropic application inference profile. The IAM policy works with a restricted role.
9. The web search switch is hidden on the Bedrock hub.
10. Subscription regression on the raised SDK floor: a `claude-local` hub's chat, scheduled run and OAuth failover behave as before.
11. Mixed hub: the Bedrock hub runs one scheduled task with `model: claude-local/sonnet`. It answers on the subscription, has no matching CloudTrail event, and its usage row names the subscription.

The record names the release, CLI version, region, profile prefix, models and the limits found.

## Decisions (20 November 2026)

1. **Tier versions.** Each tier is pinned to what it means on `claude-local` at release.
2. **Explicit `claude-local/<tier>` on a Bedrock hub** runs on the subscription, as written (founder). Bare tiers follow the hub's provider. See "Bare names follow the hub" above.
3. **Region prefix.** In ap-south-1 the default stays `apac.`, which keeps calls in the geography. If a pinned tier has no `apac.` profile, doctor fails and names it. Hubzoid never switches to `global.` on its own. An operator who accepts cross-geography routing sets `ANTHROPIC_BEDROCK_REGION_PREFIX=global` in `.env`. For Isha, that choice is theirs to make if they adopt Bedrock.
4. **Non-Claude proof model:** Amazon Nova Pro (founder).
5. **Release:** 2.0.9, with the MCP parity work (founder, 20 November 2026).

**The provider is always the user's choice** (founder). Where a hub runs never decides its model. A hub hosted on AWS, including Amazon Bedrock AgentCore, can keep `MODEL=claude-local` and the subscription token with no Bedrock key, use Bedrock, or mix the two per task. Bedrock is never turned on because the box is on AWS. Hosting on AgentCore is a separate question from the model provider. It is not tested, and AgentCore would need its own hosting validation, like Railway and Maritime, before Hubzoid claims it.

## Review

Codex (gpt-6.1-sol, high) reviewed the first draft once on 20 November 2026. All eleven findings were checked against the code and accepted:

1. Overrides escaping Bedrock in `complete_once`, the judge and stations.
2. Chat catalog ids bypassing translation.
3. Delegate classification changing `claude-local` behaviour if engines were merged.
4. Repricing aggregate tokens at one model's rate, and the missed `claude_complete` and `claude_station` usage paths.
5. Unpriced costs never reaching a dollar limit, and `usage.record` estimating them.
6. Routing every application profile ARN to Claude.
7. `ListInferenceProfiles` needing `Resource: "*"` (confirmed in the AWS reference).
8. Doctor's lookups by resource type, and strict checks on unused tiers.
9. LiteLLM's `us-west-2` fallback against the CLI's `us-east-1`.
10. Diagnostics reading `MODEL` from the environment only.
11. Proof gaps.

Finding 1 asked for the explicit `claude-local/...` question to be closed before the build. The founder closed it the other way: explicit ids are used as written, and the guarantee is per call (no call mixes credentials or falls back to another provider), not per hub. One change was narrower than suggested: for cost, the plan keeps the CLI's figure and compares it with Cost Explorer instead of repricing per model. Skipped: nothing.

## Build notes (20 November 2026)

- **SDK floor.** claude-agent-sdk 0.2.162 is the first release that bundles Claude Code 2.1.285 (its changelog), so the floor is `>=0.2.162`.
- **Tier pins** come from the bundled CLI's own model table (`provider_ids.bedrock`): Opus 5.5 (`anthropic.claude-opus-5-5`), Sonnet 5.5 (`anthropic.claude-sonnet-5-5`) and Haiku 4.5 (`anthropic.claude-haiku-4-5-20251001-v1:0`, the newest Haiku the CLI lists for Bedrock), with the region's prefix.
- **LiteLLM** is now 1.102 on this branch. Its region order (ARN, `AWS_REGION_NAME`, `AWS_REGION`, the boto3 session, else `us-west-2`) is unchanged, so Hubzoid resolves the region in that order for both engines and passes it to LiteLLM as `aws_region_name`. `supports_prompt_caching` is true for Claude and Nova and false for Llama in 1.102.4.
- **Failover.** `_query_with_failover` reads the provider from the call's own options (`CLAUDE_CODE_USE_BEDROCK=1` in its child environment), so the pool is never built for a Bedrock call.
- **Delegates.** A Claude model on Bedrock is its own engine label (`claude-bedrock`) in `handover`, so engine equality already means provider equality. A bare sub-agent model follows the hub only on a Bedrock hub, which keeps `claude-local` classification exactly as before.
- **Dollar limits.** A scope with a dollar limit and a Bedrock row with no reported cost refuses further Bedrock Claude calls (`unpriced_usage`).
- **Not done here:** a load-time warning for chat catalog entries on another provider. They are refused when a turn names them, with a message.

## Build review (20 November 2026)

Codex (gpt-6.1-sol, high) reviewed the build once. Accepted and fixed:

1. A subscription call on a Bedrock hub with `CLAUDE_CODE_USE_BEDROCK` also set by hand stayed on Bedrock. Now a `claude-local` call on a hub whose model is `bedrock/...` blanks the flag and the AWS credentials. Without that hub model, a hand-set flag is the older manual setup and is kept.
2. Such a call with no subscription token or login used an inherited API key. It now fails with a message naming the token.
3. A chat choice on another engine (`openai/...`, `codex-local`, a non-Claude Bedrock model) reached the Claude runtime. Every choice is now resolved against the hub and refused unless it is Claude on the same provider.
4. `bedrock/converse/<arn>` lost its ARN's region, and doctor skipped the lookup for LiteLLM routes. Both now use the id after the route prefix.
5. Doctor counted a station with `engine: claude-local` as using a Bedrock tier.

Skipped, with reasons:

- Counting Bedrock rows with no cost and no tokens as unknown spend. Such a row is a call that failed before reporting usage, mostly before reaching AWS. Blocking every later dollar-limited call on it would stop a hub after any error. Rows with tokens and no cost still block.
- Mapping a bare Bedrock id on a `claude-local` hub to a subscription pin. There is no reliable mapping, and it would change `claude-local`. A `claude-local` hub uses `bedrock/<id>` to reach Bedrock.
- The full final-query matrix for chat, schedules and judges. The focused tests cover the child environment at `query()` for `call_llm`, stations, mixed calls and failover, and construction plus the per-turn clone for chat. Real subscription checks passed on Claude Code 2.1.285. The AWS end-to-end run waits for a test account.

Also found against a real account: ap-south-1 offers the 5.5 models only as `global.` profiles and has an `in.` prefix, so the id rules accept any geography prefix and the docs say what that region offers.

## References

- Claude Code on Amazon Bedrock, environment variables and prompt caching docs (links above).
- `RawTools/mastra-main/mastracode/sdk/src/providers/amazon-bedrock-gateway.ts`: treats Bedrock as authenticated when AWS chain signals are present and resolves credentials through the standard provider chain, region defaulting to `us-east-1`. We check and show instead of assuming.
- `RawTools/agno-main/libs/agno/agno/models/aws/claude.py`: Claude on Bedrock through a boto3 session or a Bedrock API key, default id `global.anthropic.claude-sonnet-4-5-20250929-v1:0`. Same credential sources as here.
