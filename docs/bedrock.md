# Amazon Bedrock

Run a hub's models through your own AWS account. It is an opt-in add-on:
`MODEL=claude-local` with a Claude subscription stays the default and does not
change. Choosing Bedrock is always yours, wherever the hub runs. A hub on an AWS
box can keep the subscription, use Bedrock, or mix the two per task.

Choose Bedrock when model traffic and billing must stay in your AWS account,
when you already pay for AWS, or when you need a specific region.

## Turn it on

In the hub's `.env`:

```dotenv
MODEL=bedrock/sonnet
AWS_REGION=ap-south-1
# credentials: an instance or task role (preferred), or
# AWS_PROFILE=hubzoid
```

Then `hubzoid doctor`, and `hubzoid run` (or `hubzoid restart <hub>`).

| `MODEL=` | Runs on | What Hubzoid sends |
|---|---|---|
| `bedrock/sonnet`, `bedrock/opus`, `bedrock/haiku` | Claude Agent SDK | The tier, pinned to a Bedrock id (below) |
| `bedrock/<Bedrock Claude id>`, for example `bedrock/apac.anthropic.claude-sonnet-4-6` or `bedrock/eu.anthropic.claude-sonnet-4-6[1m]` | Claude Agent SDK | That id |
| `bedrock/arn:aws:bedrock:…:inference-profile/<id>` or `…:foundation-model/<id>` | Decided by the id inside the ARN | That ARN |
| `bedrock/arn:aws:bedrock:…:application-inference-profile/<id>` | Decided by the profile's model, read with `GetInferenceProfile` when the hub starts | That ARN |
| `bedrock/<any other id>`, for example `bedrock/apac.amazon.nova-pro-v1:0` | OpenAI Agents SDK through LiteLLM | Unchanged |
| `bedrock/converse/<id>` and LiteLLM's other route prefixes | OpenAI Agents SDK through LiteLLM | Unchanged. This keeps a Claude model on LiteLLM on purpose |

Claude on Bedrock uses the same Claude runtime as `claude-local`: the same
tools, skills, sub-agents, approvals and chat.

## The subscription and Bedrock together

A bare name follows the hub's model. An id that names its backend is used as
written.

- On a Bedrock hub, `model: opus` in a scheduled task, `hub.call_llm(model="opus")`,
  a judge model, a station or a sub-agent runs on Bedrock.
- On the same hub, `model: claude-local/opus` runs on the Claude subscription.
  That needs `CLAUDE_CODE_OAUTH_TOKEN` (or a `claude` login) for the hub, and fails
  with a clear message without one. It never falls back to Bedrock.
- On a `claude-local` hub, `model: bedrock/opus` sends that one task to Bedrock.
  It needs a region and AWS credentials, and never falls back to the subscription.

Each call gets only its own credentials. A Bedrock call's `claude` process gets
the AWS credentials, and the subscription token, its backup tokens and any
Anthropic key are blanked. A subscription call gets the token, and the AWS
credentials are blanked, as before. Each usage row names who served the call
(`bedrock/...` or the Claude model).

A sub-agent runs as its own Claude sub-agent only on the same provider as the
main agent, because it shares the main agent's process. On another provider it
is loaded as instructions for the main agent, and doctor's `agents.mode` says so.

Chat model choices (`chat.models.catalog`) must run on the agent's provider. On a
Bedrock hub list `bedrock/opus` or a Bedrock id. A choice on another provider is
refused for that turn with a message.

## Region

A Bedrock hub needs one region, or it does not start. Hubzoid reads, in order: the
region in an ARN model, `AWS_REGION_NAME`, `AWS_REGION`, `AWS_DEFAULT_REGION`,
then the AWS profile's region. It gives that region to both the Claude CLI and
LiteLLM, which would otherwise fall back to different ones (`us-east-1` and
`us-west-2`). Doctor shows the region and where it came from.

## Tier pins

`opus`, `sonnet` and `haiku` mean on Bedrock what they mean on `claude-local`
with this release (Opus 5.5, Sonnet 5.5 and Haiku 4.5), as cross-region
inference profiles for your region: `us.` for `us-*`, `eu.` for `eu-*`, `apac.`
for `ap-*`, `us-gov.` for `us-gov-*`, otherwise `global.`.

What an account is offered differs by region, so check before you choose. In
ap-south-1 (checked 20 November 2026) the 5.5 models are offered only as
`global.` profiles, the `apac.` profiles cover older models only, and an `in.`
(India) prefix offers Sonnet 5, Opus 5 and Haiku 4.5. So a hub there either sets
`ANTHROPIC_BEDROCK_REGION_PREFIX=global`, or keeps traffic in India with pins
such as `MODEL=bedrock/in.anthropic.claude-sonnet-5` and
`ANTHROPIC_DEFAULT_OPUS_MODEL=in.anthropic.claude-opus-5`.

- `ANTHROPIC_BEDROCK_REGION_PREFIX=global` uses `global.` profiles. Those can
  route outside your region's geography, so Hubzoid never switches to them on
  its own. If a tier has no profile for your prefix, doctor fails and lists the
  prefixes the account has.
- `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL` or
  `ANTHROPIC_DEFAULT_HAIKU_MODEL` in `.env` replaces one tier's pin.

Hubzoid also turns off the CLI's switch to an older model when access is
lost (`CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK`), so an unattended run fails
loudly instead.

Only `.env` configures a hub. Hubzoid runs the CLI without settings files, so
what Claude Code's `/setup-bedrock` writes to `~/.claude/settings.json` does
not apply.

## Credentials

The standard AWS chain: an instance or task role, `AWS_PROFILE`, access keys, or
a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` (which the Claude CLI prefers when
set). Prefer a role. The `claude` process and the stdio MCP servers it starts
inherit these credentials, so give the role Bedrock permissions only, or use a
Bedrock API key rather than account keys.

On a Bedrock hub, `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` in the
environment are ignored for Bedrock calls, and doctor says so. Credential
failover between subscription tokens does not apply: retries and quotas are
AWS's.

## IAM policy

`bedrock:ListInferenceProfiles` has no resource type, so it needs its own
statement on `"*"`. Replace the account, region and ids with yours:

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

Add `amazon.*` to the first statement for Nova. `sts:GetCallerIdentity` needs no
grant. Test the policy with a role that has only it, not with administrator
credentials, which hide a missing grant. The first call to an Anthropic model
in an account may also need the model's use-case form in the Bedrock console.

## Caching and cost

- **Caching.** Claude caches prompts on Bedrock as on the subscription.
  `HUBZOID_CACHE_TTL` (default `1h`, or `5m`) and `HUBZOID_DISABLE_PROMPT_CACHE`
  work the same way. On LiteLLM, only families that cache are marked (Claude and
  Nova, not Llama, for example).
- **Cost.** Each Claude call records the Claude CLI's own cost figure. If the
  CLI reports none, the row's cost stays empty instead of an estimate, and a
  dollar limit in `budgets.yaml` that already holds such a row refuses further
  Bedrock calls until an administrator reviews it. Token and turn limits always
  apply. Regional profiles (`us.`, `eu.`, `apac.`) cost about 10 percent more
  than `global.`. Compare one day's usage with AWS Cost Explorer before relying
  on the recorded dollars.
- Use one AWS account, or an application inference profile, per deployment to
  see its cost in AWS.

## What differs from the subscription

- **No web search.** Bedrock has no web search tool, so the chat switch does not
  show, and doctor warns when `retrieval.web.search` is `native`.
- **No credential failover**, as above.
- `ANTHROPIC_BEDROCK_SERVICE_TIER` passes through to the CLI. Not tested by
  Hubzoid. Guardrails are not supported.
- `codex-local` does not run on Bedrock.

## Checking it

`hubzoid doctor` shows the region and its source, the AWS identity, and whether
the model (and each tier a task, sub-agent, station or chat choice uses) exists
for the account. A denied lookup is reported as a missing discovery permission,
separate from inference access. The model reply check in Settings (Diagnostics)
makes one real call, which proves inference access.

## Troubleshooting

| Message | Fix |
|---|---|
| A Bedrock hub needs an AWS region | Set `AWS_REGION` in the hub's `.env`. |
| `…` was not found in `<region>` | The model or profile is not offered there. Use another prefix (doctor lists them) or region. |
| On-demand throughput isn't supported | Use the inference profile id (`apac.anthropic…`), not the bare model id. |
| AccessDeniedException on a call | Add the `Invoke` statement for that profile and its foundation model, and enable model access in the Bedrock console. |
| Could not read the application inference profile | Grant `bedrock:GetInferenceProfile` on it, or write `bedrock/converse/<arn>` for LiteLLM. |

## Set it up with Claude Code or Codex

Paste this into Claude Code or Codex in the hub's folder:

> Read docs/bedrock.md in the Hubzoid package. Set this hub to run on Amazon
> Bedrock: add `MODEL=bedrock/sonnet` and `AWS_REGION=<my region>` to `.env`
> and keep everything else. Tell me which AWS credentials the hub will use and
> the IAM policy it needs. Then run `hubzoid doctor` and fix what it reports,
> except IAM or console changes, which you tell me to make.
