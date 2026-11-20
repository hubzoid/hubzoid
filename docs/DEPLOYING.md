# Deploying Hubzoid

`hubzoid run` is the production entry point. It serves Hubzoid's own web app,
runs every hub of the deployment and restarts a hub that stops. Wrap it in
systemd or a container platform, and put a reverse proxy in front of its
public port for TLS.

```bash
cd /opt/hubzoid/agents                 # the folder with hubzoid.yaml, or one hub folder
hubzoid doctor .                       # check before you start
hubzoid run --host 0.0.0.0 --port 3080 --no-open
```

For accounts, owners and permissions, start with
[ADMINISTRATION.md](ADMINISTRATION.md). Moving a 1.x deployment off Open WebUI
is in [upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed).
Test a copy of the deployment before a production upgrade.

## Choose a path

| Path | Use when |
|---|---|
| [A. Native venv and systemd](#path-a-native-venv-on-a-single-linux-box) | One Linux box with one to three agents. Cheapest and simplest. Pick this unless you have a reason not to. |
| [B. Docker](#path-b-docker) | `pip install` fails on your target OS (Python version, missing system libraries), or you want the reviewed dependency set. |
| [C. ECS, Kubernetes or another orchestrator](#path-c-ecs-kubernetes-other-orchestrators) | Your organization requires infrastructure as code or a managed orchestrator, or you want each agent as its own service. The image from Path B is the entry point. |
| [D. A hosting provider (Railway, Maritime)](#path-d-a-hosting-provider) | You want no server of your own. Not yet verified on either provider's live service. |

## Supported topologies

| Topology | Databases | Notes |
|---|---|---|
| One hub on one machine (`hubzoid run` in the hub folder) | SQLite files under `<hub>/.hubzoid/` | The quick start. Back up with `hubzoid backup`. |
| Several hubs behind one web app on one machine (`hubzoid run` with a `hubzoid.yaml`) | One shared SQLite operational database in the deployment's state folder (`.hubzoid/`, or `--data-dir`), one SQLite DBOS file per hub | The common production shape. Each hub runs and restarts on its own. |
| Either of the above with PostgreSQL | `DATABASE_URL` for Hubzoid and DBOS | For production that needs managed backups or a separate database server. |
| The Docker image | SQLite on a volume, or PostgreSQL | See Path B. |
| Each hub as its own service behind one web app (ECS, Kubernetes) | PostgreSQL | One edge service runs `hubzoid run` with the deployment file. Each hub runs `hubzoid run <hub> --behind <public URL>`. See [Run an agent as its own service](#run-an-agent-as-its-own-service). |

Not supported:

- Two processes serving the same hub at once. Each hub has one bridge.
  Workflows and chat replies are guarded by the database, but live chat
  updates and the supervisor's files are not shared between copies yet. Set
  your orchestrator to stop the old copy before it starts the new one.
- Several machines sharing one set of SQLite files, for example over NFS or
  EFS. Use PostgreSQL when processes run on different machines.
- Several hubs sharing one SQLite DBOS file. `hubzoid run` refuses this.

## Settings reference

Set these in the deployment's `.env` (next to `hubzoid.yaml`), the process
environment, or a hub's `.env` for that hub only.

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_AUTH` | off | `true` turns sign-in on. Required for any deployment other people reach. See [authentication](auth.md). |
| `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK` | off | With sign-in off, only a request from this machine is the local owner. `true` keeps the earlier rule: anyone who reaches the port acts as the owner. Use it only on a network you trust. |
| `HUBZOID_PUBLIC_URL` | unset | The HTTPS address people open. In `hubzoid.yaml` deployments, set `public_url` there instead. |
| `HUBZOID_HOST` | `127.0.0.1` | Interface the public port binds to (or `--host`). `0.0.0.0` needs sign-in. The bridge stays on 127.0.0.1. |
| `PORT`, `BRIDGE_PORT` | `3080`, first free from `8000` | Public port and bridge port. Leave `BRIDGE_PORT` unset in a `hubzoid.yaml` deployment. |
| `HUBZOID_TRUSTED_PROXIES` | loopback and private networks | Proxies whose `X-Forwarded-For` is believed: addresses or networks, comma-separated, or `*` when the port is reachable only through your proxy. Setting it replaces the private-network default. |
| `HUBZOID_SECRET_KEY` | a `secret.key` file | The deployment key. See [secrets](secrets.md#the-deployment-key). |
| `HUBZOID_REQUIRE_SECRET_KEY` | off | `true` refuses to start without `HUBZOID_SECRET_KEY` instead of making a key file. `hubzoid secrets new-key` prints a key for your secret store. |
| `DATABASE_URL` | SQLite | PostgreSQL for Hubzoid and DBOS. |
| `HUBZOID_OPERATIONAL_DB`, `HUBZOID_DBOS_DB` | `DATABASE_URL` | Keep Hubzoid's tables and DBOS in separate databases. |
| `HUBZOID_DB_POOL_SIZE`, `HUBZOID_DB_MAX_OVERFLOW` | 5, 10 | Connections each process keeps, and how many more under load. Every bridge, the edge, the workflow owner's lock connection and DBOS add up: keep the total under the database's limit. |
| `HUBZOID_DB_POOL_RECYCLE` | no recycling | Seconds before a connection is replaced. Set it below the idle timeout of the database or a proxy. |
| `HUBZOID_DB_POOL_TIMEOUT` | 30 | Seconds to wait for a free connection. |
| `HUBZOID_DB_CONNECT_TIMEOUT` | driver default | Seconds to open a connection. |
| `HUBZOID_LOG_FORMAT` | `text` | `json` writes one JSON object per log line, for CloudWatch Logs Insights or a SIEM. |
| `HUBZOID_AUDIT` | `off` | `log`, `db` or `both` records every tool call, sign-in and admin change. See [the audit log](AUDIT.md). |
| `HUBZOID_BACKUPS` | `weekly` | `daily`, `weekly` or `off`. See [scheduled backups](BACKUP.md#scheduled-backups). |
| `HUBZOID_IN_APP_EDITING` | off | See [in-app editing](#in-app-editing). |
| `HUBZOID_DISABLE_WEB_SEARCH` | off | `true` removes the `web_search` tool from the agent. |
| `HUBZOID_DISABLE_HTTP_GET` | off | `true` removes the `http_get` tool from the agent. |
| `HUBZOID_DISABLE_PROMPT_CACHE` | off | `true` stops marking the agent's instructions for the provider's prompt cache. |
| `HUBZOID_CACHE_TTL` | `1h` | How long the provider keeps the prompt cache. `5m` is cheaper per write. |
| `HUBZOID_PASSWORD_HASH_CONCURRENCY` | `4` | How many password checks run at once. Lower it on a small machine. |
| `HUBZOID_FORWARDED_PROTO_HEADER` | unset | A header your front door sets to the scheme people used, trusted over `X-Forwarded-Proto` (`CloudFront-Forwarded-Proto` behind CloudFront and a plain-HTTP load balancer). Set it only when nothing else can reach the port. |

Agent turns on Amazon Bedrock AgentCore ([AWS](deploy/aws.md)). `hubzoid deploy init aws` sets all of these:

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_RUNNER` | `local` | `agentcore` runs each agent turn and its tools on an AgentCore runtime, one microVM per chat. The hub service keeps chats, sign-in, channels, workflows, schedules and approvals. |
| `HUBZOID_AGENTCORE_RUNTIME_ARN` | unset | The runner's runtime. |
| `HUBZOID_AGENTCORE_QUALIFIER`, `HUBZOID_AGENTCORE_REGION` | `DEFAULT`, the ARN's region | The runtime's endpoint and region. |
| `HUBZOID_MCP_HOST` | `local` | `agentcore`: the hub's MCP server also runs as an AgentCore MCP runtime, with this hub's sign-in. |
| `HUBZOID_MCP_AGENTCORE_URL`, `HUBZOID_MCP_RUNTIME_NAME` | unset | That runtime's address, or its name to look the address up. Sign-in issues tokens for it. |
| `HUBZOID_ROLE` | unset | Set inside the runtimes only: `runner` or `mcp` (`hubzoid runner start`). Such a process never migrates the database, schedules or answers channels. |
| `HUBZOID_IMAGE` | unset | The image tag. A runner runs a call only for the same Hubzoid version and image as the hub service. |

Telemetry settings are in [OBSERVABILITY.md](OBSERVABILITY.md).

## SQLite or PostgreSQL

SQLite is the default and needs no setup. It suits one machine with one bridge
per hub. Choose PostgreSQL for a managed database, point-in-time backups, or
processes on more than one machine.

```bash
pip install "hubzoid[postgres]"
# in the hub's environment, or the deployment's .env next to hubzoid.yaml:
DATABASE_URL=postgresql+psycopg://hubzoid:<password>@db.internal:5432/hubzoid
```

Use the `postgresql+psycopg://` form. Hubzoid installs the psycopg 3 driver
only.

| Tables | Owner | Upgraded by |
|---|---|---|
| `hz_*` (accounts, chats, connectors, access, audit, usage, workflow state) | Hubzoid | Hubzoid migrations |
| schema `dbos` (runs and checkpoints) | DBOS | DBOS |

Accounts, sessions, conversations, permissions and personal MCP connector
credentials use Hubzoid's operational database. Hosted MCP authenticates
through Hubzoid OAuth. Registered bridges fail on conflicting database
settings instead of falling back to another store.

- **SQLite in production.** The supported envelope is one host with durable
  local storage, one bridge per hub, one shared operational database and
  separate per-hub DBOS databases. Do not add bridge replicas or put these
  files on NFS. `hubzoid doctor` fails SQLite on a network filesystem
  (`db.network_fs`).
- **Sizing.** There is no certified maximum hub or user count. Size the
  machine for the actual workload.
- **Rehearse.** Before rollout, rehearse backup and restore, and killing a
  workflow mid-step, on a copy of the deployment. Completed steps must remain
  checkpointed and interrupted side effects must be idempotent.
- **Watch** disk space, backup age, workflow health and lock errors.
- **Moving from SQLite to PostgreSQL** is not automated in this release.
  Start PostgreSQL deployments fresh, or copy the data yourself. Back up
  PostgreSQL with `pg_dump` ([BACKUP.md](BACKUP.md)).

## Configuration layers and AWS secrets

Settings come in layers. Each layer is a local file or environment, plus an
optional JSON secret in AWS Secrets Manager. A deployment that names no secret
is unchanged: hub `.env` files load as before, boto3 is not imported and
nothing calls AWS.

### Precedence

Lowest first. A later row overrides an earlier one, for the processes it
reaches.

| # | Layer | Source | Secret named by | Reaches |
|---|---|---|---|---|
| 1 | Built-in | Hubzoid defaults | | every process |
| 2a | Deployment, 0.9.x compatibility | Sign-in keys (`OAUTH_*`, `OPENID_*`, `GOOGLE_CLIENT_*`, `ENABLE_SIGNUP`, `ENABLE_LOGIN_FORM`, `ENABLE_OAUTH_SIGNUP` and similar) and `HUBZOID_PUBLIC_URL` from hub `.env` files, only when 2b and 2c do not set them | | `hubzoid run`, and the hubs it starts |
| 2b | Deployment | The deployment's process environment and the `.env` next to `hubzoid.yaml` (systemd `EnvironmentFile`, shell). Standalone `hubzoid run`: the hub `.env` and the process environment | | see below |
| 2c | Deployment secret | JSON secret | `AWS_SECRET_NAME` in the deployment's environment. Standalone: in the hub `.env` or the environment | see below |
| 3a | Hub | `<hub>/.env` | | that hub's bridge, inbound, Slack and agent runtime |
| 3b | Hub secret | JSON secret | `HUBZOID_HUB_SECRET_NAME` in `<hub>/.env` | same as 3a |
| 4a | Restricted | `<hub>/restricted/.env` | | that hub's restricted tools, in the bridge process |
| 4b | Restricted secret | JSON secret | `HUBZOID_RESTRICTED_SECRET_NAME` in `restricted/.env` | same as 4a |

- **Within a layer, the secret wins over the file.** A key a secret overrides
  is logged by name, never by value.
- **Across layers, the more specific layer wins.** A hub in a deployment of
  several hubs applies the deployment secret before its own `.env`.
- **A standalone hub treats its `.env` as the deployment layer.** There the
  deployment secret is applied after the `.env` and wins over it: load `.env`,
  then let the secret override.
- **One hub cannot redirect the deployment.** In a deployment of several hubs,
  `AWS_SECRET_NAME` in a hub `.env` is ignored with a warning. Use
  `HUBZOID_HUB_SECRET_NAME` for a hub's own secret.
- **Secret names are read from their own layer only.**
  `HUBZOID_HUB_SECRET_NAME` is read from `<hub>/.env`.
  `HUBZOID_RESTRICTED_SECRET_NAME` is read from `restricted/.env`.
- `DATABASE_URL`, `DATABASE_SCHEMA` and `HUBZOID_OPERATIONAL_DB` fail on a
  conflict with the deployment manifest. A hub value of a deployment-wide key,
  such as an `OAUTH_*_ENCRYPTION_KEY`, that differs from the deployment's is a
  startup warning, not a failure, because older hub `.env` files carry these
  keys.
- Open WebUI's settings (`WEBUI_*` and the others listed in
  [upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed)) are
  never inherited. Those that decide sign-in stop the start wherever they are
  set. The rest are named as warnings.

### What each process gets

- **`hubzoid run`** reads the deployment secret once at start. It loads hub
  `.env` files only to plan ports and names, and never fetches a hub or
  restricted secret. It records the deployment secret's name and region
  (never a value) in `deployment.json` as `deployment_secret`.
- **Bridges** take only these keys from the deployment secret:
  `HUBZOID_ADMIN_EMAIL`, `HUBZOID_ADMIN_PASSWORD`, `HUBZOID_SECRET_KEY`,
  `HUBZOID_WORKFLOW_USER`, the `HUBZOID_SMTP_*` and `HUBZOID_EMAIL_DELIVERY`
  settings, `HUBZOID_ARTIFACT_LINK_DAYS`, `HUBZOID_ARTIFACT_ALLOW_ORIGINS`,
  every `OAUTH_*_ENCRYPTION_KEY`, `DATABASE_URL`, `DATABASE_SCHEMA`,
  `HUBZOID_OPERATIONAL_DB`, `HUBZOID_PUBLIC_URL`, `HUBZOID_OTEL_ENDPOINT` and
  `OTEL_*`. `hubzoid run` names the other keys at start (they stay with the
  supervisor). A bridge turns the deployment `HUBZOID_PUBLIC_URL` into its own
  `<url>/b/<hub>` address. Each bridge fetches its own hub and restricted
  secrets.
- **Hubs `hubzoid run` starts** receive the deployment values from the
  supervisor and do not fetch the deployment secret again.
- **Agent child processes.** The `claude` CLI, and the stdio MCP servers it
  starts, get these variables blanked when present:
  - the secret names: `AWS_SECRET_NAME`, `HUBZOID_HUB_SECRET_NAME`,
    `HUBZOID_RESTRICTED_SECRET_NAME`
  - AWS credential material: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
    `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN`,
    `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, `AWS_CONTAINER_CREDENTIALS_FULL_URI`,
    `AWS_CONTAINER_AUTHORIZATION_TOKEN`, `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`,
    `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_ROLE_ARN`, `AWS_BEARER_TOKEN_BEDROCK`.
    These are kept for a Claude call on Bedrock (`MODEL=bedrock/...`, see
    [Amazon Bedrock](bedrock.md)), because the CLI then signs in with them.
    Hubzoid sets `CLAUDE_CODE_USE_BEDROCK` for that call's child itself, so do
    not set it by hand. A hub that still sets it with `MODEL=claude-local`
    keeps working as before.
  - Hubzoid service secrets: `BRIDGE_API_KEYS`, `HUBZOID_ARTIFACT_SECRET`,
    `HUBZOID_SECRET_KEY`, `HUBZOID_ADMIN_PASSWORD`, `HUBZOID_SMTP_USERNAME`,
    `HUBZOID_SMTP_PASSWORD`, every `OAUTH_*_ENCRYPTION_KEY`, `DATABASE_URL`,
    `HUBZOID_OPERATIONAL_DB`, `HUBZOID_DBOS_DB`, and the retired Open WebUI
    credentials `WEBUI_SECRET_KEY`, `WEBUI_ADMIN_PASSWORD` and
    `HUBZOID_GATEWAY_ADMIN_PASSWORD`
  - every key set by `restricted/.env` or the restricted secret. A key the hub
    layers also set gets the hub layer's value instead of a blank.

  The CLI's own `ANTHROPIC_*` and `CLAUDE_*` keys always pass, so
  `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` keep working.
  `AWS_PROFILE`, `AWS_REGION` and the AWS config file paths pass, because they
  select credentials rather than hold them. Other hub `.env` keys pass
  unchanged, so a stdio MCP server that reads, for example, `SLACK_BOT_TOKEN`
  still works. An MCP server that needs a blanked value can name it in
  `.mcp.json` (`"env": {"TOKEN": "${TOOL_TOKEN}"}`), which the bridge fills in.
  Restricted tools run inside the bridge process for every runtime, so they
  still see their values. The Codex runtime starts from a short allowlist, and
  the OpenAI runtime's stdio MCP servers get the MCP SDK's minimal
  environment. Agents run as the same OS user as the bridge, so this narrows
  what children inherit. It is not a filesystem sandbox.

### Using AWS Secrets Manager

Store each secret as a flat JSON object of `NAME: value` pairs:

```json
{"HUBZOID_SECRET_KEY": "...", "HUBZOID_ADMIN_PASSWORD": "...", "GOOGLE_CLIENT_SECRET": "..."}
```

Name it in the deployment's `.env`, and in hub and restricted `.env` files:

```bash
AWS_SECRET_NAME=prod/hubzoid/deployment
AWS_REGION=ap-south-1
HUBZOID_HUB_SECRET_NAME=prod/hubzoid/sales               # in sales/.env
HUBZOID_RESTRICTED_SECRET_NAME=prod/hubzoid/sales-tools  # in sales/restricted/.env
```

- Strings are used as they are. Numbers and booleans become strings (`true`,
  `false`). `null`, nested values and binary secrets are refused.
- A secret may not set `AWS_*` keys, the three secret-name keys, or
  process-control keys (`PATH`, `LD_*`, `DYLD_*`, `PYTHONPATH`, `PYTHONHOME`,
  `PYTHONSTARTUP`, `NODE_OPTIONS`). Credentials never chain through a secret.
- Name the region with `AWS_REGION` (or `AWS_DEFAULT_REGION`). A secret ARN
  carries its own region.
- **Credentials** come only from boto3's default chain: the instance, task or
  pod role on AWS. Elsewhere use `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY` (plus `AWS_SESSION_TOKEN` for temporary
  credentials). Hubzoid never passes keys itself.
- **IAM.** Grant `secretsmanager:GetSecretValue` on exactly the named secret
  ARNs, plus `kms:Decrypt` when the secret uses a customer-managed KMS key.
  With one instance role, every process on the host can read every secret
  that role allows. Hubzoid scopes secrets per hub in how it loads them, not
  in IAM. Per-hub IAM isolation needs a task role per hub, or a separate OS
  user per hub.
- **Failure is fatal.** A secret that cannot be read stops the process before
  it serves, with the secret name, the layer and the AWS error class (for
  example `AccessDeniedException` or `NoCredentialsError`). Values never
  appear in messages or logs. Logs show the key count at INFO and the key
  names at DEBUG.

### Rotation

Values are read once, when a process starts. After rotating a secret,
restart:

| Secret | Restart |
|---|---|
| The deployment secret | `hubzoid run`, then every bridge |
| A hub secret | That hub's bridge, inbound and Slack processes |
| A restricted secret | That hub's bridge |

Connector tokens are encrypted, and identity assertions signed, with the
deployment key (`HUBZOID_SECRET_KEY` or the key file). Rotating or losing it
means people connect their tools again. Keep it stable.

## Check a deployment

`hubzoid doctor <hub>` checks a hub and its deployment without changing
anything: files, configuration layers and secrets, the agent build,
schedules, database schema, bridge keys, chat sign-in, the public bind, model
credentials, backup age and scheduled work. It exits 1 when any check fails.

```bash
hubzoid doctor ./alpha                      # readable
hubzoid doctor ./alpha --json               # for scripts and monitoring
hubzoid doctor ./alpha --skip-secret-fetch  # never call AWS
```

Each check has a stable id, such as `auth.bridge_keys`, `db.operational`,
`backup.age` or `scheduler.health`, and a status of `ok`, `info`, `warn` or
`fail`. New checks may be added. Existing ids are never renamed.

| Check | What it reports |
|---|---|
| `config.layers` | Every key a file or secret sets, with its layer and source. Never a value. |
| `secrets.deployment`, `secrets.hub`, `secrets.restricted` | Reads each named secret once to prove it is reachable. |
| `secrets.names` | A secret name set where it is ignored. |
| `auth.google_merge` | `GOOGLE_CLIENT_ID` set without `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`, which Google sign-in onto an existing password account needs. |
| `db.network_fs` | Fails when a SQLite database is on a network filesystem (NFS, EFS, SMB and similar). |

### Health checks

Both the public port (the edge) and each bridge answer these:

| Path | Use it for |
|---|---|
| `/healthz` | Liveness, for a container health check. It does not touch the database, so a database outage does not make the platform replace healthy tasks. |
| `/readyz` | Readiness, for a load balancer's target group. It answers 503 while the database does not answer or its schema is behind, so traffic waits and a bad deploy can roll back. On the edge it asks the bridge's readiness. |
| `/b/<hub>/healthz`, `/b/<hub>/readyz` | The same for one hub, through the edge. |
| `/healthz/workflows` | On the edge: 200 when every hub's workflow engine is healthy. |

## Path A: native venv on a single Linux box

This walkthrough uses Ubuntu 24.04 on AWS EC2. The same steps work on Debian
or RHEL-derived systems with their package manager, and on any cloud or
on-premises VM. Sizing rule of thumb: 2 GB RAM minimum for one agent, 4 GB
comfortable for two or three on the same box.

### 1. Server prep

```bash
sudo apt update && sudo apt install -y \
  python3.12 python3.12-venv build-essential git curl
sudo useradd -r -m -d /opt/hubzoid -s /bin/bash hubzoid
```

The reverse proxy comes in step 6.

### 2. Firewall or security group

Allow only:

- 22/tcp from your admin IP (SSH)
- 80/tcp from anywhere. Most TLS certificate issuers, including Let's
  Encrypt, use it for the HTTP-01 challenge, and the proxy usually redirects
  80 to 443.
- 443/tcp from anywhere

Block everything else inbound. Hubzoid's bridge port (8000 by default) binds
to 127.0.0.1, so it is not reachable from outside the box even if the firewall
is permissive.

### 3. Install Hubzoid and the agents repo

```bash
sudo -iu hubzoid
git clone git@github.com:your-org/your-hub-agents.git agents
cd agents
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The agents repo follows the layout in the [main README](../README.md): one
`requirements.txt` at the top (pin a specific Hubzoid version), one folder per
hub.

### 4. Configure each hub's `.env`

```bash
cd /opt/hubzoid/agents/devops-agent     # your hub's folder
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Required for production:

- `MODEL=`: a hosted provider with an API key
  (`MODEL=anthropic/claude-haiku-4-5` with `ANTHROPIC_API_KEY`,
  `MODEL=openrouter/...` with `OPENROUTER_API_KEY`, and so on), or
  `MODEL=claude-local` to run on a Claude Pro or Max subscription with a
  long-lived token. See
  [5b](#5b-running-claude-local-in-production-subscription-no-api-key).
- `HUBZOID_AUTH=true`, with an administrator created before people arrive:
  `hubzoid admin create you@example.com <hub> --owner`. Open its one-time
  sign-in link and choose a password. See [authentication](auth.md). Sign-in
  is off by default. With it off, only a request from this machine is the
  local owner, so other people get no access.
- `HUBZOID_PUBLIC_URL=https://devops.agents.example.com`, the HTTPS address
  people open. Keep the generated deployment `secret.key` protected and backed
  up separately ([backup](BACKUP.md)).
- `PORT=3080` and `BRIDGE_PORT=8000` for a standalone hub, each unique per hub
  on the same box. In a `hubzoid.yaml` deployment, leave them out: `hubzoid
  run` assigns bridge ports.

DNS: point `devops.agents.example.com` (an A record) at the box's public IP
before starting the reverse proxy. A proxy that issues certificates itself
(Caddy, certbot with nginx) needs DNS to resolve first.

### 5. systemd unit (one unit for every hub)

`hubzoid run` supervises every hub of the deployment in its own process. A hub
that stops is started again, and one hub is restarted, stopped or reloaded
with `hubzoid restart|stop|start|reload <hub>` while the others keep
answering. So one unit runs the whole deployment. Write it from the deployment
folder (the one with `hubzoid.yaml`, or the hub folder for one hub):

```bash
cd /opt/hubzoid/agents
sudo .venv/bin/hubzoid service install --port 3080 --host 0.0.0.0 --user hubzoid --write
```

Without `--write` the command prints the unit and a hint to save it. It
writes `/etc/systemd/system/hubzoid.service`:

```ini
[Unit]
Description=Hubzoid
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/hubzoid/agents
ExecStart=/opt/hubzoid/agents/.venv/bin/hubzoid run --port 3080 --host 0.0.0.0 --no-open
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=180
User=hubzoid

[Install]
WantedBy=multi-user.target
```

Add what your box needs under `[Service]`, for example:

```ini
# claude-local runs the `claude` CLI. The systemd default PATH
# excludes ~/.local/bin, so name it here. Harmless for API-key hubs.
Environment=PATH=/opt/hubzoid/.local/bin:/usr/local/bin:/usr/bin:/bin
OOMPolicy=stop
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# /opt/hubzoid (the service user's home) must be writable: hubs keep state
# under agents/, and claude-local writes session and cache files under ~/.claude.
ReadWritePaths=/opt/hubzoid
# Do NOT enable ProtectHome: hiding ~/.claude breaks MODEL=claude-local
# (the CLI cannot read its token and config). Left off on purpose.
ProtectKernelTunables=true
ProtectKernelModules=true
RestrictSUIDSGID=true
LockPersonality=true
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hubzoid
hubzoid status              # every hub: ready, restarting, failed or stopped
hubzoid logs <hub> -f       # one hub's output (also in journalctl -u hubzoid)
```

`TimeoutStopSec=180` leaves running replies their two-minute grace period on
a stop.

### 5b. Running claude-local in production (subscription, no API key)

`MODEL=claude-local` runs on a Claude Pro or Max subscription instead of a
metered API key. It works without an interactive login, with a long-lived
subscription token:

1. On any machine signed in to the subscription, make a token:

   ```bash
   claude setup-token        # prints an OAuth token valid for about a year (sk-ant-oat01-...)
   ```

   This is not an API key (`sk-ant-api03-...`) and is not billed per token.
   Usage draws on the subscription's limits. Treat it as a secret. Rotate by
   running `claude setup-token` again.

2. Put it in the hub's `.env` (already `chmod 600`) next to the model:

   ```bash
   MODEL=claude-local
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```

   Hubzoid loads `.env` into the environment, and the `claude` CLI reads
   `CLAUDE_CODE_OAUTH_TOKEN` itself. Nothing is passed on the command line,
   and no `claude login` is needed on the box.

3. Check the two things the default hardening would otherwise break:

   - The `claude` CLI must be installed for the service user and on the
     unit's PATH (the `Environment=PATH=` line). Install it as the `hubzoid`
     user (`sudo -iu hubzoid`, then your usual Claude Code install) and
     confirm `which claude` resolves under `~/.local/bin` or `/usr/local/bin`.
   - `claude` writes session and cache files under `$HOME/.claude`, so the
     service user's home must be writable (`ReadWritePaths=/opt/hubzoid`) and
     not hidden (`ProtectHome` left unset).

Make a new token when it expires. Heavy automated use shares the
subscription's rate limits with interactive Claude Code use. If latency or
limits are a problem, `MODEL=claude-local/haiku` answers about three times
faster to the first token, or switch that hub to a metered API-key provider.

#### Claude credential failover

Available from 1.3.2. Add up to two optional backups next to the primary in
the hub's `.env` or the host's environment. In a deployment of several hubs,
use the same slots for hubs that should share these credentials. Do not put
tokens in hub knowledge, chat, source control or agent instructions.

```dotenv
CLAUDE_CODE_OAUTH_TOKEN=<primary credential>
CLAUDE_CODE_OAUTH_TOKEN_SECONDARY=<first backup>
CLAUDE_CODE_OAUTH_TOKEN_TERTIARY=<second backup>
```

This needs an explicit primary and Claude OAuth authentication only. Remove
conflicting API key, auth token or other provider settings first. Hubzoid
refuses ambiguous authentication rather than charge another account without
saying so. Blank backup slots are ignored. Identical token values are tried
once. Without backups, sign-in works as before.

- Every attempt uses the same model, hub, caller identity and tools.
- Only structured Claude usage or rate-limit failures and authentication
  failures switch credentials. Warnings that a limit is near do not. Unknown
  errors, bad inputs, refusals, tool or MCP failures, timeouts and
  provider-wide outages do not.
- If the request fails before any content or tool activity, the next
  credential is tried within the same request. After work starts, the partial
  turn ends with the existing error, and the next request can use a backup.
  Never assume an interrupted action was undone, and never repeat it
  automatically.
- Each request tries at most three distinct credentials. SDK-internal retries
  still apply, within the calling surface's deadline. Failover can add
  latency, but does not wait hours for a limit to reset.
- A limited credential stays unavailable until its reported reset, or five
  minutes when no usable reset is given. One request probes recovery while
  others keep using backups. An inconclusive probe waits another five
  minutes. A crashed probe's lease expires after five minutes. Only a
  successful final SDK result restores the earlier credential's priority.
  There is no round-robin switching.
- A rejected login stays disabled until its token is replaced. Restarting
  with the same token does not clear the failure.
- Hubs sharing the operational database share cooldowns, keyed by a token
  digest. Raw credentials are never stored in this metadata. Separate
  standalone stores cannot coordinate: use one deployment (`hubzoid.yaml`) or
  a deliberately shared operational store.
- There are no alerts or admin screens for this. Logs name the slot (0 is the
  primary, 1 and 2 the backups), the failure category and the recovery,
  without printing token values.

To replace a token, change only its slot and restart the bridges. A new token
value starts eligible. A rejected old value stays disabled if put back. The
replacement must have access to the same model. Changing credentials does not
change hub files, conversation storage, caller access or tool grants. A bad
replacement can fail authentication or share an exhausted quota, so verify
one read-only request after restarting. To turn failover off, remove both
backup variables and restart. Other runtimes are unaffected.

Backups must be authorized for the deployment. Different tokens do not give
independent quota, and failover does not help in a Claude-wide outage.
Anthropic's [Agent SDK guidance](https://code.claude.com/docs/en/agent-sdk/overview)
requires prior approval for third-party products offering claude.ai login or
rate limits. This feature is not a claim of approved shared subscription
hosting.

### 6. Reverse proxy + TLS (optional, recommended)

Recommended for any deployment people reach over the internet. Skip it if the
hub is internal only (behind a VPN, on localhost, or through an SSH tunnel).
Without TLS, chat traffic is plain text, Google sign-in refuses to redirect,
and browsers drop secure cookies.

| Proxy | Trade-off |
|---|---|
| Caddy | One package, automatic Let's Encrypt certificates and renewal, the shortest config. A good default if you have no preference. |
| nginx with certbot | More configuration, certificate renewal by certbot. Pick it if you already run nginx. |
| AWS ALB, GCP load balancer or Cloudflare Tunnel | Managed TLS, no certificate work on the box. Higher cost or an extra account. Pick it if you already use the platform. |

Whatever you pick, the proxy must:

- Pass the client's address in `X-Forwarded-For`. A proxy on the same box is
  believed, and one on a private network too while `HUBZOID_TRUSTED_PROXIES`
  is unset. Setting it lists exactly which proxies to believe. A proxy or CDN
  on public addresses (Cloudflare without a tunnel, for example) must be
  listed in `HUBZOID_TRUSTED_PROXIES`, or the sign-in limits count everyone as
  the proxy's address.
- Terminate TLS on 443 and redirect 80 to 443.
- Forward to `127.0.0.1:<PORT>` (the public port of `hubzoid run`).
- Pass Server-Sent Events through without buffering. Streamed replies rely on
  it.
- Carry WebSocket upgrades (most proxies do this by default).

Example `/etc/caddy/Caddyfile` for two standalone hubs:

```caddyfile
devops.agents.example.com {
    reverse_proxy 127.0.0.1:3080 {
        flush_interval -1
        transport http {
            read_timeout 600s
        }
    }
}

sales.agents.example.com {
    reverse_proxy 127.0.0.1:3081 {
        flush_interval -1
        transport http {
            read_timeout 600s
        }
    }
}
```

`flush_interval -1` turns off buffering for streamed replies. `read_timeout
600s` covers long replies. Install with `sudo apt install caddy`, then `sudo
systemctl reload caddy`. In nginx, use `proxy_buffering off;` and pass the
`Upgrade` header. On an ALB this is the default on HTTP/1.1 target groups.

Then open `https://devops.agents.example.com`. You should see the sign-in
screen.

### 7. Backup

Each agent takes a verified backup weekly by itself, into its
`.hubzoid/backups` (`HUBZOID_BACKUPS=weekly|daily|off`, see
[BACKUP.md](BACKUP.md#scheduled-backups)). Copy those archives off the
machine.

For your own schedule, turn that off and use cron. `hubzoid backup` saves
each agent's databases, chat data and runtime state to one verified archive
while chat keeps working. The hub's markdown (AGENTS.md, skills/, knowledge/)
is in git and is not included. Save this as `/etc/cron.daily/hubzoid-backup`
(`chmod +x`):

```bash
#!/bin/sh
set -e
date=$(date +%F)
cd /opt/hubzoid/agents
for hub in */AGENTS.md; do
  name=$(dirname "$hub")
  .venv/bin/hubzoid backup "$name" --out "/var/backups/hubzoid-${name}-${date}.tar.gz"
done
# Ship offsite, for example:
# aws s3 cp /var/backups/ s3://your-bucket/hubzoid/ --recursive --exclude '*' --include "hubzoid-*-${date}.tar.gz"
find /var/backups -name 'hubzoid-*.tar.gz' -mtime +14 -delete
```

To restore: stop the service, run `hubzoid restore <archive>`, start it. Keep
the deployment key the same across restores. See [BACKUP.md](BACKUP.md) for
what is saved, moving to a new machine and PostgreSQL.

### 8. Updating hubzoid

Database migrations do not support rolling updates across versions on this
path. Back up first, then upgrade in place:

```bash
sudo -iu hubzoid
cd /opt/hubzoid/agents
for hub in */AGENTS.md; do .venv/bin/hubzoid backup "$(dirname "$hub")" --out "/tmp/pre-upgrade-$(dirname "$hub")-$(date +%F).tar.gz"; done
# Edit requirements.txt: hubzoid==<new-version>
source .venv/bin/activate
pip install -r requirements.txt
exit
sudo systemctl restart hubzoid
```

Expect 10 to 60 seconds of downtime while the hubs start (three at a time).
After changing only one hub's files, `hubzoid reload <hub>` restarts that hub
alone. See [UPGRADING.md](UPGRADING.md) for release notes.

### 9. Slack, WhatsApp, Telegram and webhooks (optional)

Channels turn on by a file in the hub's `channels/` folder, never a flag, and
run with their hub (restarting the hub restarts them). The Slack assistant
app uses Socket Mode, so it needs no public URL:

```yaml
# <hub>/channels/slack-assistant.yaml
version: 1
id: slack-assistant
provider: slack_assistant    # SLACK_BOT_TOKEN and SLACK_APP_TOKEN in the hub's .env
enabled: true
```

`provider: whatsapp_inbound`, `telegram` and `webhook` turn on the inbound
server for WhatsApp (`WHATSAPP_*`), Telegram (`TELEGRAM_*`) and generic
webhooks (`WEBHOOK_INBOUND_*`). They arrive on the public port at
`/webhooks/<hub>/...`. A file whose secrets are missing stays off, and the
start and `hubzoid channels list` say which are missing. See
[channels](channels.md) and the [Slack walkthrough](slack.md).

## Several hubs on one address (`hubzoid.yaml`)

One web app, one sign-in and one Settings page across all agents. Put a
`hubzoid.yaml` in the folder that holds the hub folders:

```yaml
version: 1
public_url: https://hub.example.com
sign_in: true
owner: admin@example.com
hubs:                 # the first also answers at /
  - sales-agent
  - support-agent
  - finance-agent
brand:
  identity:
    name: Example Hubs
```

Secrets and switches for the whole deployment (`HUBZOID_SECRET_KEY`,
`DATABASE_URL`, sign-in client secrets, `ENABLE_SIGNUP`, mail) go in the
`.env` next to it, which wins over the process environment. Each hub's `.env`
holds only that hub's settings. One that sets a deployment setting
differently stops the start. Then, in that folder:

```bash
export HUBZOID_ADMIN_PASSWORD='a-strong-bootstrap-password'   # first start only
hubzoid run --host 0.0.0.0 --port 3080
```

On a fresh deployment, sign in as the configured owner. Add people and grant
**Use this agent** and restricted capabilities in Settings. Administration
alone does not grant every agent or every tool.

- Hub folder names and model ids must be unique. Collisions stop the start
  with an explanation.
- Bridge ports are assigned from 8000 up, skipping ports another program
  uses. A `BRIDGE_PORT` in a hub's `.env` is still honoured. `--bridge-port`
  is for `hubzoid run` on a single hub folder only.
- Hub commands in this folder (`hubzoid audit`, `hubzoid secrets list`,
  `hubzoid channels ...`) use the deployment's `public_url`, `sign_in`,
  `owner` and `in_app_editing`. In a deployment of one hub they act on that
  hub. With several hubs, name one: `hubzoid audit sales-agent`.
- Artifact downloads route per hub: each bridge advertises
  `<public-url>/b/<hub-slug>`, so its download links come back through the
  edge to the right bridge. Leave `HUBZOID_PUBLIC_URL` unset in each hub's
  `.env`. `public_url` in `hubzoid.yaml` sets it for every hub.
- A deployment shares one sign-in by design. Separate sign-in realms per team
  need separate deployments.

### Control a running deployment

Each hub is its own process. A hub that exits is started again after 1
second, doubling to 60 seconds. After 5 failed starts in 10 minutes it is
marked failed and stays stopped until `hubzoid start <hub>`. Meanwhile its
pages and API answer 503 "This agent is restarting", and the other agents
keep answering. A broken `tools_local` file fails only its hub.

Run these from the deployment folder, or with `--data-dir`:

| Command | What it does |
|---|---|
| `hubzoid status` | Each hub's state, restarts, last exit, and whether a reload is needed |
| `hubzoid restart <hub> [<hub>...]` | Drain, then restart those hubs one after the other |
| `hubzoid reload <hub>` | Check the hub folder, then restart it. A folder with problems changes nothing. |
| `hubzoid stop <hub>`, `hubzoid start <hub>` | Stop a hub until started again, or start a stopped or failed one |
| `hubzoid logs <hub> [-f]` | That hub's output |

A restart drains first. Webhooks to that hub wait (senders retry), its 1.x
channels finish what they acknowledged, it takes no new replies, and running
replies get `--grace` seconds (default 120) before the process stops. A reply
cut off then is marked interrupted and its queue is kept. Workflow runs and
approvals waiting for a person resume after a restart on the same code. An
upgrade cancels runs started by older code, so let them finish first.

Brand, chat defaults and diagnostics limits are sections of `hubzoid.yaml`
that every agent reads again when the file changes. Agents' own files
(instructions, skills, channels, connectors, `.env`, `tools_local/`) take
effect with `hubzoid reload <hub>`. `hubzoid status` says when a hub needs
one.

SIGTERM or Ctrl-C stops every hub (each with its grace period), then the
edge. If `hubzoid run` itself is killed, its children notice and stop, so no
process keeps a port. systemd (`KillMode=mixed` in the unit above) or the
container platform restarts the deployment.

`hubzoid gateway <hubs>` still works for one release: it prints the
`hubzoid.yaml` that says the same and runs it. `--no-bridges` and separate
units per hub are gone. `hubzoid restart` is the per-hub restart. To move a
running gateway, see [upgrading](UPGRADING.md#one-run-mode).

### In-app editing

Changing an agent's files from the browser is Beta and off by default. That
covers proposing and approving hub changes (instructions, knowledge, skills),
the agent's `remember` and `propose_hub_change` tools, and saving channels,
settings, approval rules and budgets in Settings. People still read channels,
settings and approval rules there, and the CLI and your editor work as
before. An agent's instruction files show in Settings only while it is on,
under **Configuration**, **Instructions**.

```yaml
in_app_editing: true      # in hubzoid.yaml, or HUBZOID_IN_APP_EDITING=true
```

Restart `hubzoid run` (not just one hub) after changing it.

Turn it on where the agent folders are a working copy someone looks after,
such as a laptop or a server checkout you commit from. Settings then marks
these controls Beta. Files that come from an image (`HUBZOID_DATA_DIR` set)
are never written, whatever the switch says. Change them in the repository
and deploy again.

Connectors are the exception. Organization administrators add, edit, offer
and delete them on an agent's **Connectors** tab with the switch off too.
Each one is a YAML file under `connectors/`, so on a server checkout commit
the new file (or add it in the repository) before the next pull or deploy.

### Branding

The brand is the `brand:` section of `hubzoid.yaml`, with its images under
`branding/` next to it. Settings shows it read-only. Each hub's own
`branding/` logo and name identify it in the agent picker. See
[branding](branding.md).

## Path B: Docker

Build the image from the `Dockerfile` at the repository root. It installs the
checked-out source with the reviewed dependency set in `requirements.lock`, so
the image version is the version you checked out.

```bash
git checkout v<version>          # the release you want
docker build -t hubzoid:<version> .

docker run -d --restart unless-stopped \
  --name devops-agent \
  -p 3080:3080 \
  -v "$PWD/devops-agent:/hub" \
  --env-file "$PWD/devops-agent/.env" \
  hubzoid:<version>
```

- The image runs `hubzoid run /hub`. Pass other arguments after the image
  name, for example `hubzoid:<version> run /hub --port 3080`. Channels turn
  on by files in the hub.
- The image listens on all interfaces (`HUBZOID_HOST=0.0.0.0`), so the hub's
  `.env` needs `HUBZOID_AUTH=true`, or the start stops. To try it without
  sign-in, publish the port on this machine only (`-p 127.0.0.1:3080:3080`)
  and set `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`.
- State persists in the mounted hub folder.
- Publish only port 3080. The edge there serves the chat app, artifact
  downloads, the portal and MCP. The bridge stays on 127.0.0.1 inside the
  container.
- Put a reverse proxy in front of port 3080 as in
  [step 6](#6-reverse-proxy--tls-optional-recommended).
- `MODEL=claude-local` does not work inside the image (no `claude` CLI). Use
  an API key provider.
- On Linux, the container runs as an unprivileged user, so the mounted hub
  folder must be writable by it (for example `chown -R 999:999 my-hub`, or
  run with `--user "$(id -u):$(id -g)"`).

Each release is also published as a multi-architecture image
(`ghcr.io/hubzoid/hubzoid:<version>`, amd64 and arm64), built by the release
pipeline from the tagged source. Building it yourself gives the same result.

### Docker Compose: SQLite or PostgreSQL

`docker/docker-compose.yml` does the same as the `docker run` above, with
sign-in on by default.

```bash
# SQLite (default): state lives in the mounted hub folder
HUB_PATH=$PWD/my-hub docker compose -f docker/docker-compose.yml up -d

# PostgreSQL: adds a database container. Its port is not published.
HUB_PATH=$PWD/my-hub POSTGRES_PASSWORD=<letters and digits> \
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.postgres.yml up -d
```

`HUBZOID_IMAGE` picks the image (default `hubzoid:local`, built from this
checkout). Operator commands run inside the container:

```bash
docker compose -f docker/docker-compose.yml exec hubzoid hubzoid doctor /hub
docker compose -f docker/docker-compose.yml exec hubzoid hubzoid backup /hub --out /hub/backup.tar.gz
```

To upgrade: back up, stop, pull or build the new image and start again. The
new version upgrades its tables at start ([UPGRADING.md](UPGRADING.md)).

## Path C: ECS, Kubernetes, other orchestrators

The image from Path B is the entry point. Use PostgreSQL (RDS or another
managed PostgreSQL). SQLite on a network filesystem is not safe. On ECS
Fargate, never put SQLite databases (`.hubzoid/*.db` and the deployment's
state folder) on EFS. `hubzoid doctor` fails such a setup.

On AWS, `hubzoid deploy init aws` writes a CloudFormation template for a
single hub on ECS with RDS and EFS, and can run its agent turns on Amazon
Bedrock AgentCore: [deploy/aws.md](deploy/aws.md).

Choose one of two shapes:

| Shape | How | Trade-off |
|---|---|---|
| One service | `hubzoid run` with the deployment file, as in Path A. The web app and every hub in one task. | Simple. Every hub restarts with the task. |
| One service per hub | An edge service, plus one service per hub with `--behind` | Each hub has its own task size, image, secrets, restarts and deploys. |

Set these on hosted services:

```bash
HUBZOID_REQUIRE_SECRET_KEY=true     # every service must share HUBZOID_SECRET_KEY
HUBZOID_LOG_FORMAT=json
HUBZOID_AUDIT=log                   # optional, see AUDIT.md
HUBZOID_DB_POOL_RECYCLE=300         # below your database's or proxy's idle timeout
```

Size `HUBZOID_DB_POOL_SIZE` and `HUBZOID_DB_MAX_OVERFLOW` so all services
together stay under the database's connection limit
([settings](#settings-reference)). If you put RDS Proxy in front of the
database, check first how it treats session advisory locks: the workflow
owner and migrations hold one on a direct connection.

Point the container health check at `/healthz` and the load balancer's target
group at `/readyz` ([health checks](#health-checks)).

### Run an agent as its own service

The edge service is the only public door. It runs `hubzoid run` with the
deployment file behind the load balancer. For each hub that runs as its own
service, that hub's `.env` names where it runs:

```bash
HUBZOID_BRIDGE_UPSTREAM=http://support.hubzoid.local:8000
HUBZOID_INBOUND_UPSTREAM=http://support.hubzoid.local:8100   # only with WhatsApp, Telegram or webhooks
```

Both take `http(s)://host[:port]` with no path, query or credentials. The
edge starts no process for that hub. It routes the hub's paths there and
checks its `/healthz` every 5 seconds. After two missed checks people see
"This agent is restarting" and the other hubs keep working. `hubzoid restart
<hub>` says to restart it in its own service.

Each hub service runs:

```bash
hubzoid run /hubs/support --behind https://hub.example.com
```

with these in its environment:

| Setting | Why |
|---|---|
| `HUBZOID_BRIDGE_HOST=0.0.0.0` | Listen on the task's network address, not only on loopback. The inbound server, when the hub has one, listens on the same address. |
| `HUBZOID_TRUSTED_PROXIES=10.0.0.0/16` | The edge's network. The hub believes the client address the edge forwards from there only, so sign-in limits and audit records see each person's address. Required when the bridge listens on the network. |
| `HUBZOID_AUTH=true` | A hub on the network refuses to start with sign-in off, unless `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`. |
| `DATABASE_URL` (or `HUBZOID_OPERATIONAL_DB`) | The same database as the edge. Accounts, sessions and conversations are shared. |
| `HUBZOID_SECRET_KEY` | The same deployment key as the edge, so the hub can check who is asking and read stored secrets. |
| `HUBZOID_DEPLOYMENT` | The edge's `deployment.json` on the shared volume, so the hub knows the deployment (its slug, the other agents, sign-in). |
| `BRIDGE_API_KEYS` (in the hub's `.env`) | A random key, not the public `dev`, because the bridge listens on the network. The edge reads the same one. |
| `BRIDGE_PORT`, `HUBZOID_INBOUND_PORT` | Optional. Default the first free port from 8000, and 8100. Match the upstream addresses. |

`--behind` tells the hub what the edge tells a hub it starts: its public
address (`<public URL>/b/<hub>`), its MCP address, the web app mode and
sign-in. Missing settings stop the start with a message that names them. The
service ends when any of its processes ends, so the platform restarts it.

Share two things between the edge and every hub service, at the same paths in
each: the hub folders, and the edge's data folder (`--data-dir`, which holds
`deployment.json`). EFS works for both, with the databases in PostgreSQL. The
edge writes `deployment.json` and a pointer in each hub's `.hubzoid/` folder
at start, so it needs write access. The edge reads each hub's `AGENTS.md`,
`.env` flags, branding and webhook routes.

### Deploys

Hubzoid runs one copy of each hub. Configure the orchestrator to stop the old
task before it starts the new one (ECS: `minimumHealthyPercent` 0 and
`maximumPercent` 100 for each hub service). A brief overlap is safe for
workflows and chat replies, which the database guards, but people connected
to the old copy see their live reply only when it ends.

A new version upgrades the database when it starts, while the old version may
still run. From 2.0.9, a release only adds tables and columns, and removes
one a release after the code stopped using it, so the old version keeps
working during the deploy. Read [UPGRADING.md](UPGRADING.md) for releases
before that.

### What each process keeps in memory

Some settings are cached for a short time in each process, so a change made
in one service reaches the others within this time:

| What | For up to |
|---|---|
| Access decisions | Not cached: a grant or removal applies on the next check everywhere |
| Sign-in settings | 5 s |
| Embedded apps | 3 s |
| Group membership from `access.py` | 60 s |
| A person's connections | 60 s |
| Single sign-on provider metadata | 1 h |
| Secrets from AWS Secrets Manager | Until the service restarts |

The Claude runtime resumes a chat from the session files of the task that
answered before. A new task starts the conversation again from its stored
history. The answer is the same, but that one turn costs more.

For multi-hub setup, account ownership, migration preview, cutover and
rollback, see [Administration](ADMINISTRATION.md).

## Path D: a hosting provider

`hubzoid deploy init railway|maritime` writes a provider's deployment files,
and `hubzoid deploy verify <public URL>` checks the result.
[deploy/](deploy/README.md) has each provider's steps. Neither path is yet
verified on the provider's live service.

## Local Codex runtime

`MODEL=codex-local` works with the reviewed Codex CLI versions 0.147.0 and
0.163.0. Another version is refused with the versions to install. Set
`HUBZOID_CODEX_BIN` to use a Codex binary that is not first on `PATH`.

- Follow the [provider setup](providers.md#local-codex) for file-backed login
  as the service account, model pinning and isolation details.
- The standard Docker image does not include Codex. Use a custom image with
  the pinned CLI and persistent private credential storage for the service
  account. Do not mount an operator's entire home.
- Hubzoid uses temporary per-request configuration and saves refreshed login
  tokens back to the configured credential store. It must be writable by the
  service account and kept out of hub data, backups shared with users, and
  source control.
- CLI or model upgrades require repeating the real-CLI tool isolation tests.

## Troubleshooting

Run `hubzoid doctor <hub>` first.

| Symptom | Check |
|---|---|
| Scheduled tasks never run, and the log says the workflow engine did not start | `hubzoid doctor` `deps.sqlite`: on Python 3.12 the engine needs SQLite 3.42 or later. Use a Python build with a newer SQLite (python.org, uv, Homebrew, Debian 13, Ubuntu 24.04) or PostgreSQL. |
| `systemctl start` succeeds but the web app is not reachable | `hubzoid status`, then `hubzoid logs <hub>` (or `journalctl -u hubzoid -f`) for the ready line and startup errors. |
| The start stops: sign-in is off and the host is not loopback | Set `HUBZOID_AUTH=true` and create an administrator, or keep `--host 127.0.0.1`. |
| Other people get no access while sign-in is off | Expected: only a request from this machine is the local owner. Turn sign-in on. |
| One agent answers "This agent is restarting" | `hubzoid status`: it is restarting with backoff, or failed (with the reason). Fix it, then `hubzoid start <hub>`. The other agents keep running. For a hub in its own service, check that service and its `/healthz`. |
| `hubzoid run <hub> --behind` stops with a list of problems | Each line names the missing setting. See [Run an agent as its own service](#run-an-agent-as-its-own-service). |
| Everyone is locked out of sign-in together | The proxy's address is counted for everyone. List it in `HUBZOID_TRUSTED_PROXIES`. |
| The start stops: `HUBZOID_REQUIRE_SECRET_KEY is on` | Set `HUBZOID_SECRET_KEY` to the deployment's key ([secrets](secrets.md#the-deployment-key)). |
| `/readyz` answers 503 | The database does not answer, or its schema is behind. The response body says which. |
| `db.network_fs` fails | A SQLite file is on NFS or EFS. Move it to local disk or use PostgreSQL. |
| The start stops naming an Open WebUI setting (`WEBUI_AUTH`, `WEBUI_URL`, `HUBZOID_UI=openwebui` and similar) | Rename or remove it as the message says. See [upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed). |
| TLS certificate never issues | DNS for the hostname has not propagated, or the box cannot reach the certificate issuer. Check the proxy's logs (`journalctl -u caddy`, `/var/log/nginx/error.log`). |
| Several agents conflict at start | Two hubs pin the same `BRIDGE_PORT` in their `.env`. Remove the line and let `hubzoid run` assign ports. |
| Chats are missing after an upgrade | Check the deployment database. Chats from a 1.x Open WebUI deployment need `hubzoid migrate openwebui`. Keep the pre-upgrade backup. |
| The Slack app loops on restart | `hubzoid logs <hub>`. Usually a missing token, or a stale bot token after reinstalling the app. See [Slack](slack.md). |
