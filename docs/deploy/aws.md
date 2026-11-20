<!-- Generated from hubzoid/deploy/providers/aws/README.md: `hubzoid deploy init aws` writes this page into a hub as deploy/aws/README.md. Edit the template, not this file. -->

# Deploy this hub on AWS, with agent turns on AgentCore

> **Not yet verified on AWS.** These steps are being checked end to end in a
> test account. Report what you find.

`hubzoid deploy init aws` wrote three files: a `Dockerfile` (the Hubzoid image,
ghcr.io/hubzoid/hubzoid:<version>, plus this hub's files and AWS's OpenTelemetry
package), `deploy/aws/stack.yaml` (one CloudFormation template) and this page.
You need an AWS account, the AWS CLI, and nothing else on your machine: AWS
CodeBuild builds the image.

## What the stack creates

| Part | What it is |
|---|---|
| Hub service | ECS Fargate (arm64) running `hubzoid run`: the web app, sign-in, chats and their history, channels, workflows, schedules, approvals and the database migrations. Behind an Application Load Balancer and CloudFront, which gives the HTTPS address |
| Runner | An Amazon Bedrock AgentCore runtime (`<hub>_runner`) running agent turns and their tools, each chat in its own microVM. Turned on with `Runner=agentcore` (the default) |
| MCP runtime | Optional (`McpHost=agentcore`): the hub's MCP server as an AgentCore MCP runtime (`<hub>_mcp`), with this hub's own sign-in |
| Database | RDS PostgreSQL, shared by the hub service and the runtimes, daily backups kept 7 days |
| Files | EFS at `/mnt/data` on every host: uploads, outputs, keys, backups |
| Image | An ECR repository and a CodeBuild project that builds the arm64 image |
| Network | A VPC with two public and two private subnets and one NAT gateway |
| Secrets | One Secrets Manager secret the hub reads at start: the database address plus the secrets you name |

The hub's own files are only read, in every host. Change them in Git, build a
new image and deploy it.

The hub service stays the owner of everything shared. A runner never migrates
the database, schedules work or answers channels. It runs one turn for one
request the hub service recorded first, and refuses any other request, even
one signed by AWS. Only the hub service's role may invoke the runner (the
runtime's resource policy).

## 1. Secrets you keep

Put these in Secrets Manager as plain text, once. The stack only reads them.

```bash
# The Claude licence token from `claude setup-token` (MODEL=claude-local).
aws secretsmanager create-secret --name hub/claude-token --secret-string "<token>"
# The deployment key (encrypts stored connections). Keep a copy outside AWS.
aws secretsmanager create-secret --name hub/key --secret-string \
  "$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
# The first administrator's password, for the first sign-in.
aws secretsmanager create-secret --name hub/admin-password --secret-string "<password>"
```

## 2. First deploy: network, database, files and the image build

```bash
aws cloudformation deploy --stack-name hub --template-file deploy/aws/stack.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides ClaudeTokenSecret=hub/claude-token \
    DeploymentKeySecret=hub/key AdminEmail=you@example.com \
    AdminPasswordSecret=hub/admin-password
```

This takes about 15 minutes (the database). Nothing runs yet: `ImageTag` is empty.

## 3. Build the image

From the hub folder:

```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name hub \
  --query "Stacks[0].Outputs[?OutputKey=='BuildBucket'].OutputValue" --output text)
TAG=$(git rev-parse --short HEAD)
git archive --format=zip -o /tmp/source.zip HEAD && aws s3 cp /tmp/source.zip "s3://$BUCKET/source.zip"
aws codebuild start-build --project-name hub-image \
  --environment-variables-override name=IMAGE_TAG,value=$TAG
```

Wait for the build to succeed (`aws codebuild batch-get-builds`, or the console).

## 4. Second deploy: the hub service and the runner

```bash
aws cloudformation deploy --stack-name hub --template-file deploy/aws/stack.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides ImageTag=$TAG
```

Parameters you set before keep their values. The stack prints `Url`. Open it
and sign in as the administrator, then remove `AdminPasswordSecret`.

## 5. Check it

```bash
hubzoid deploy verify https://<Url>
```

In the app, Settings > Diagnostics shows "Where agent turns run": the runner's
version, answered without a model call. On the command line, inside the hub
service (`aws ecs execute-command ... --command "hubzoid runner hello --json"`).

## Code workflows

A hub with code workflows (`workflows/`) deploys with `CodeWorkflows=true`.
They run in the hub service. A workflow's `hub.call_agent` step runs on the
runner. Markdown schedules (`schedule/*.md`) run either way, their agent on the
runner.

## MCP on AgentCore

Deploy with `McpHost=agentcore` (and `PublicHttps=true`, the default). The
stack prints `McpUrl`. Give that address to Claude or another MCP client. It
signs in with this hub's own sign-in, as at `<Url>/mcp`. AgentCore checks the
token came from this hub; Hubzoid checks the person's access, as on the hub
service. Each MCP session runs in its own microVM.

## Updating

Build a new image (step 3) and deploy it (step 4) with the new tag. The hub
service restarts first and upgrades the database. Then the runtimes update.
A turn already running finishes on the old version.

Changing a secret you keep: update it in Secrets Manager, then deploy the stack
again with a new `SecretsVersion` (any value, for example the date). The stack
assembles the deployment secret again and restarts the hub service and the
runtimes, which read it at start. The values of the secrets you keep go into
that JSON as they are, so they must not contain `"` or `\`. Tokens from
`claude setup-token`, Fernet keys and generated passwords never do.

During an update that adds a database migration, the runtimes update before
the hub service restarts and upgrades the database. For those few minutes a
runner refuses calls from the previous release, so agent turns fail with a
clear message. Update outside busy hours.

## Turning the runner off

Deploy with `Runner=local`. The hub service runs agent turns itself again. No
data moves. `Runner=agentcore` turns it back on.

## Observability

Runner and MCP sessions send traces to CloudWatch through AWS's OpenTelemetry
package in the image. Turn on CloudWatch Transaction Search once per account
(CloudWatch > Application Signals > Transaction Search), then open
Bedrock AgentCore > Observability. Logs: `/hubzoid/hub` (hub service)
and `/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT` (runtimes).
`HUBZOID_OTEL=off` in a runtime's environment turns the instrumentation off.

## Limits

- One turn on the runner may stream for 55 minutes. A scheduled task with a
  longer `timeout` is reported by `hubzoid doctor`.
- A runtime session stops after 15 idle minutes or 8 hours. The next message
  of that chat starts a new microVM (a few seconds). The chat itself, its
  history and files are kept by the hub service, the database and EFS.
- Workflows run in the hub service. A tool on the runner that starts a workflow
  is refused with a clear message: start it from the hub service.
- AgentCore allows 4 KB of environment variables per runtime. Settings with
  values go in the deployment secret, never in the runtime's environment.
- A runner runs a call only for the same Hubzoid version and image as the hub
  service that sent it (`HUBZOID_IMAGE`, the image tag).
- Bedrock models: the stack's roles have no Bedrock permissions. Add
  `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` to the
  runner's and the hub service's roles before choosing one.

## Costs

The parts that bill while the stack exists, at on-demand prices: the NAT
gateway (about USD 1.10 a day plus data), the load balancer (about USD 0.55 a
day), RDS `db.t4g.micro` (about USD 0.45 a day), the Fargate task (0.5 vCPU,
1 GB, about USD 0.40 a day), EFS and ECR storage, CloudWatch logs, and
CloudFront requests. AgentCore bills per second of active CPU and memory while
a session works, not while it waits on the model.

## Removing it

`aws cloudformation delete-stack --stack-name hub`. The database
leaves a final snapshot and the EFS file system is kept (`DeletionPolicy`).
Delete them yourself once you no longer need the data. Empty the build bucket
and the image repository first, or the stack cannot delete them.
