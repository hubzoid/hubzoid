# Amazon Bedrock

Run a hub's models through your own AWS account. Use Bedrock when model traffic
and billing must stay in your AWS account, when you already pay for AWS, or when
you need a specific region.

Bedrock is opt in. `MODEL=claude-local` with a Claude subscription stays the
default and does not change. A hub can use the subscription, Bedrock, or both
for different tasks, wherever it runs.

## Quick start

In the hub's `.env`:

```dotenv
MODEL=bedrock/sonnet
AWS_REGION=ap-south-1
# Credentials: an instance or task role (preferred), or
# AWS_PROFILE=hubzoid
```

Then run `hubzoid doctor`, and `hubzoid run` (or `hubzoid restart <hub>`).

Claude on Bedrock uses the same Claude runtime (the Claude Agent SDK) as
`claude-local`, with the same tools, skills, sub-agents, approvals and chat. It
needs `claude-agent-sdk` 0.2.162 or later, which Hubzoid 2.0.9 installs.

## Model ids

| `MODEL=` | Runs on | What Hubzoid sends |
|---|---|---|
| `bedrock/sonnet`, `bedrock/opus`, `bedrock/haiku` | Claude Agent SDK | The tier's pinned Bedrock id (see [Region and tier pins](#region-and-tier-pins)) |
| `bedrock/<Bedrock Claude id>`, for example `bedrock/apac.anthropic.claude-sonnet-4-6` or `bedrock/eu.anthropic.claude-sonnet-4-6[1m]` | Claude Agent SDK | That id |
| `bedrock/arn:aws:bedrock:…:inference-profile/<id>` or `…:foundation-model/<id>` | Decided by the id inside the ARN | That ARN |
| `bedrock/arn:aws:bedrock:…:application-inference-profile/<id>` | Decided by the profile's model, read with `GetInferenceProfile` when the hub starts | That ARN |
| `bedrock/<any other id>`, for example `bedrock/apac.amazon.nova-pro-v1:0` | OpenAI Agents SDK through LiteLLM | Unchanged |
| `bedrock/converse/<id>` and LiteLLM's other route prefixes | OpenAI Agents SDK through LiteLLM | Unchanged. This keeps a Claude model on LiteLLM on purpose |

An isolated agent ([stations](stations.md)) can set `engine: bedrock`. Its
default model is then `bedrock/sonnet`.

## Settings

All settings go in the hub's `.env`. Hubzoid runs the Claude CLI without
settings files, so what Claude Code's `/setup-bedrock` writes to
`~/.claude/settings.json` does not apply.

| Setting | Default | What it does |
|---|---|---|
| `AWS_REGION` | none | The region. A Bedrock hub without one does not start. `AWS_REGION_NAME`, `AWS_DEFAULT_REGION` and the AWS profile's region also work (order below). |
| `AWS_PROFILE`, access keys, `AWS_BEARER_TOKEN_BEDROCK` | the standard AWS chain | Credentials. `AWS_BEARER_TOKEN_BEDROCK` is a Bedrock API key, which the Claude CLI prefers when set. |
| `ANTHROPIC_BEDROCK_REGION_PREFIX` | from the region | The inference profile prefix for the tiers, for example `global`. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | the tier pin | Replaces one tier's Bedrock id. |
| `HUBZOID_CACHE_TTL` | `1h` | Prompt cache lifetime, `1h` or `5m`. |
| `HUBZOID_DISABLE_PROMPT_CACHE` | off | `true` turns prompt caching off. |
| `ANTHROPIC_BEDROCK_SERVICE_TIER` | not set | Passed through to the Claude CLI. Not tested by Hubzoid. |

## Region and tier pins

Hubzoid reads the region from, in order: the region in an ARN model,
`AWS_REGION_NAME`, `AWS_REGION`, `AWS_DEFAULT_REGION`, then the AWS profile's
region. It gives that one region to both the Claude CLI and LiteLLM, which would
otherwise fall back to different ones (`us-east-1` and `us-west-2`).

`opus`, `sonnet` and `haiku` mean on Bedrock what they mean on `claude-local`
with this release, as cross-region inference profiles for your region:

| Tier | Model | Bedrock id after the prefix |
|---|---|---|
| `opus` | Opus 5.5 | `anthropic.claude-opus-5-5` |
| `sonnet` | Sonnet 5.5 | `anthropic.claude-sonnet-5-5` |
| `haiku` | Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` |

| Region | Prefix |
|---|---|
| `us-gov-*` | `us-gov.` |
| `us-*` | `us.` |
| `eu-*` | `eu.` |
| `ap-*` | `apac.` |
| any other | `global.` |

`global.` profiles can route outside your region's geography, so Hubzoid never
switches to them on its own. Set `ANTHROPIC_BEDROCK_REGION_PREFIX=global` to use
them. If a tier has no profile for your prefix, doctor fails and lists the
prefixes the account has.

What an account is offered differs by region, so check before you choose. In
ap-south-1 (checked 20 November 2026) the 5.5 models are offered only as
`global.` profiles, the `apac.` profiles cover older models only, and an `in.`
(India) prefix offers Sonnet 5, Opus 5 and Haiku 4.5. A hub there either sets
`ANTHROPIC_BEDROCK_REGION_PREFIX=global`, or keeps traffic in India with pins:

```dotenv
MODEL=bedrock/in.anthropic.claude-sonnet-5
ANTHROPIC_DEFAULT_OPUS_MODEL=in.anthropic.claude-opus-5
```

Hubzoid also turns off the CLI's switch to an older model when access is lost
(`CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK`), so an unattended run fails
instead of quietly changing model.

## Use Bedrock and the subscription together

A bare model name follows the hub's provider. An id that names its provider is
used as written.

| Hub `MODEL` | Task `model:` | Runs on |
|---|---|---|
| `bedrock/...` | `opus`, `sonnet`, `haiku`, a `claude-*` id or a Bedrock Claude id | Bedrock |
| `bedrock/...` | `claude-local/opus` | The Claude subscription |
| `claude-local` | `opus` | The Claude subscription |
| `claude-local` | `bedrock/opus` | Bedrock |

This applies to a scheduled task's `model:`, `hub.call_llm(model=...)`, a judge
model, a station and, on a Bedrock hub, a sub-agent. A sub-agent on a
`claude-local` hub names the prefix (`claude-local/haiku`).

- A `claude-local/...` task on a Bedrock hub needs `CLAUDE_CODE_OAUTH_TOKEN` (or
  a `claude` login) for the hub. Without one it fails with a message. It never
  falls back to Bedrock.
- A `bedrock/...` task on a `claude-local` hub needs a region and AWS
  credentials. It never falls back to the subscription.
- Each call gets only its own credentials. A Bedrock call gets the AWS
  credentials, and the subscription tokens and any Anthropic key are blanked. A
  subscription call gets the token, and the AWS credentials are blanked. Each
  usage row names who served the call (`bedrock/...` or the Claude model).
- A sub-agent runs as its own Claude sub-agent only on the same provider as the
  main agent, because it shares the main agent's process. On another provider
  it is loaded as instructions for the main agent, and doctor's `agents.mode`
  says so.
- Chat model choices (`chat.models.catalog`) must run on the agent's provider.
  On a Bedrock hub, list `bedrock/opus` or a Bedrock id. A choice on another
  provider is refused for that turn with a message.

## Credentials and IAM

Use the standard AWS chain: an instance or task role, `AWS_PROFILE`, access
keys, or a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK`. Prefer a role. The
`claude` process and the stdio MCP servers it starts inherit these credentials,
so give the role Bedrock permissions only, or use a Bedrock API key rather than
account keys.

On a Bedrock hub, `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` are ignored
for Bedrock calls, and doctor says so.

The IAM policy below covers inference, the lookups doctor makes and first use of
Anthropic models. `bedrock:ListInferenceProfiles` has no resource type, so it
needs its own statement on `"*"`. Narrow the resources to your regions and
profiles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Invoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:*:*:inference-profile/apac.anthropic.*",
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:application-inference-profile/*"
      ]
    },
    {
      "Sid": "Describe",
      "Effect": "Allow",
      "Action": ["bedrock:GetInferenceProfile", "bedrock:GetFoundationModel"],
      "Resource": [
        "arn:aws:bedrock:*:*:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:application-inference-profile/*"
      ]
    },
    {
      "Sid": "List",
      "Effect": "Allow",
      "Action": ["bedrock:ListInferenceProfiles"],
      "Resource": "*"
    },
    {
      "Sid": "FirstUseOfAnthropicModels",
      "Effect": "Allow",
      "Action": ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"],
      "Resource": "*"
    }
  ]
}
```

- Change `apac.anthropic.*` to the prefix you use. Add `amazon.*` to the first
  statement for Nova.
- `sts:GetCallerIdentity` needs no grant.
- Test the policy with a role that has only it. Administrator credentials hide a
  missing grant.
- The first call to an Anthropic model in an account may also need the model's
  use case form in the Bedrock console.

## Cost and caching

- **Cost.** Each Claude call records the Claude CLI's own cost figure, and a
  resumed chat records each turn's own cost. If the CLI reports no cost, the
  row's cost stays empty instead of an estimate. A dollar limit in
  [budgets.yaml](budgets.md) whose scope already holds such a row then refuses
  further Bedrock calls (`unpriced_usage`). Token and turn limits always apply.
- Compare one day's usage with AWS Cost Explorer before relying on the recorded
  dollars. AWS prices regional profiles (`us.`, `eu.`, `apac.`) about 10 percent
  above `global.`. Check current AWS pricing for your models.
- To see one deployment's cost in AWS, use one AWS account, or an application
  inference profile, per deployment.
- **Caching.** Claude caches prompts on Bedrock as on the subscription, with
  `HUBZOID_CACHE_TTL` and `HUBZOID_DISABLE_PROMPT_CACHE`. On LiteLLM, only model
  families that cache are marked (Claude and Nova, not Llama, for example).

## What differs from the subscription

- **No web search.** Bedrock has no web search tool. The chat switch does not
  show, and doctor warns when `retrieval.web.search` is `native`.
- **No credential failover.** Failover between subscription tokens does not
  apply. Retries and quotas are AWS's.
- **No Bedrock Guardrails.** They are not supported.
- `codex-local` does not run on Bedrock.

## Check the setup

`hubzoid doctor` shows the region and where it came from, the AWS identity, and
whether the model exists for the account. It also checks each tier that a task,
sub-agent, station or chat choice uses. A denied lookup is reported as a missing
discovery permission, separate from inference access. The model reply check in
Settings (Diagnostics) makes one real call, which proves inference access.

## Troubleshooting

| Message | Fix |
|---|---|
| A Bedrock hub needs an AWS region | Set `AWS_REGION` in the hub's `.env`. |
| `…` was not found in `<region>` | The model or profile is not offered there. Use another prefix (doctor lists them) or another region. |
| On-demand throughput isn't supported | Use the inference profile id (`apac.anthropic…`), not the bare model id. |
| AccessDeniedException on a call | Add the `Invoke` statement for that profile and its foundation model, and enable model access in the Bedrock console. |
| Could not read the application inference profile | Grant `bedrock:GetInferenceProfile` on it, or write `bedrock/converse/<arn>` to run it on LiteLLM. |
| No usable AWS credentials | Use an instance or task role, `AWS_PROFILE`, access keys or `AWS_BEARER_TOKEN_BEDROCK`. |
| A dollar limit refuses Bedrock calls (`unpriced_usage`) | A Bedrock call recorded no cost. Use token or turn limits on Bedrock, or ask an administrator to review the usage. |

## Set it up with Claude Code or Codex

Paste this into Claude Code or Codex in the hub's folder:

> Read docs/bedrock.md in the Hubzoid package. Set this hub to run on Amazon
> Bedrock: add `MODEL=bedrock/sonnet` and `AWS_REGION=<my region>` to `.env`
> and keep everything else. Tell me which AWS credentials the hub will use and
> the IAM policy it needs. Then run `hubzoid doctor` and fix what it reports,
> except IAM or console changes, which you tell me to make.
