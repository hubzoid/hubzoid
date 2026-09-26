# Deploying Hubzoid to production

For the current account, owner and permission flow, start with
[ADMINISTRATION.md](ADMINISTRATION.md). The group/model-ACL procedures later in
this guide describe legacy or manually wired deployments; managed hub permissions
are changed in the Console. Test a copied deployment before a production upgrade.


`hubzoid run <hub>` is the production entry point. Wrap it in `systemd`
(or your container orchestrator of choice), and for any public
deployment put a reverse proxy in front of Open WebUI's port to handle
TLS. The reverse proxy is your choice; the rest of the walkthrough is
the same regardless.

This page covers the recommended path in detail: a single Linux box
running 1-N agents via systemd. Docker and container-orchestrator
deployments get short notes at the end.

## Picking a path

| Path | Use when |
|---|---|
| A. Native venv + systemd + a reverse proxy of your choice | 1-3 agents on a single Linux box. Cheapest and simplest. |
| B. Docker | The `pip install` dance fails on your target OS (PyAV build issues, Python-version traps, missing system libraries). |
| C. ECS / Kubernetes / other orchestrators | Your org mandates IaC or a managed orchestrator. The image from Path B is the entry point; the wiring is yours. |

If you don't have a reason to pick B or C, pick A.

## Supported topologies

| Topology | Databases | Notes |
|---|---|---|
| One hub on one machine (`hubzoid run`) | SQLite files under `<hub>/.hubzoid/` and `<hub>/.openwebui-data/` | The quick start. Back up with `hubzoid backup`. |
| Several hubs behind one chat app on one machine (`hubzoid gateway`) | One shared SQLite operational database in the gateway's `--data-dir`, one SQLite DBOS file per hub | The common production shape. |
| Either of the above with PostgreSQL | `DATABASE_URL` for Hubzoid and DBOS (and Open WebUI if you choose) | For production that needs managed backups or a separate database server. |
| The Docker image | Either SQLite on a volume or PostgreSQL | See Path B. |

Not supported:

- Two processes serving the same hub at once. Each hub has one bridge.
- Several machines sharing one set of SQLite files (for example over NFS).
  Use PostgreSQL when processes run on different machines.
- Several hubs sharing one SQLite DBOS file. The gateway refuses this.

## SQLite or PostgreSQL

SQLite is the default and needs no setup: each hub keeps its databases in its
own folder. It suits one machine and a single bridge per hub. Choose PostgreSQL
when you want a managed database, point-in-time backups, or processes on more
than one machine.

```bash
pip install "hubzoid[postgres]"
# in the environment of the hub, or of the gateway and every bridge:
DATABASE_URL=postgresql+psycopg://hubzoid:<password>@db.internal:5432/hubzoid
```

Use the `postgresql+psycopg://` form. Hubzoid installs the psycopg 3 driver
only, and Open WebUI reads the same `DATABASE_URL`.

MCP API-key authentication, Open WebUI group memberships and connected MCP
OAuth credentials are read from that same database. `DATABASE_SCHEMA`, when
set, selects Open WebUI's schema for these lookups. The gateway records both
values in its deployment manifest; conflicting bridge settings deny lookup
instead of reading another store. Read connections enforce read-only access;
only the OAuth refresh path writes a renewed encrypted token. A connection
failure never falls back to an older local `webui.db`.

With `DATABASE_URL` set, three sets of tables share that database, each
upgraded by its owner at start:

| Tables | Owner | Upgraded by |
|---|---|---|
| `hz_*` (access, audit, usage, workflow state) | Hubzoid | Hubzoid's migrations |
| schema `dbos` (runs and checkpoints) | DBOS | DBOS |
| accounts, chats, files | Open WebUI | Open WebUI |

To keep them apart, set `HUBZOID_OPERATIONAL_DB` and `HUBZOID_DBOS_DB` to
other databases. Moving an existing SQLite deployment's data to PostgreSQL is
not automated in this release: start PostgreSQL deployments fresh, or copy the
data yourself. Back up PostgreSQL with `pg_dump` ([BACKUP.md](BACKUP.md)).

## Configuration layers and AWS secrets

Settings come in layers. Each layer is a local file or environment, plus an
optional JSON secret in AWS Secrets Manager. Nothing changes for a deployment
that names no secret: hub `.env` files load exactly as before, boto3 is not
imported and nothing calls AWS.

### Precedence

Lowest first. A later row overrides an earlier one, for the processes it
reaches.

| # | Layer | Source | Secret named by | Reaches |
|---|---|---|---|---|
| 1 | Built-in | Hubzoid defaults | | every process |
| 2a | Deployment, 0.9.x compatibility | Open WebUI and sign-in keys (`WEBUI_*`, `OAUTH_*`, `ENABLE_SIGNUP` and similar) from hub `.env` files, only when 2b and 2c do not set them | | the gateway and Open WebUI, and the bridges the gateway launches (as in 0.9.x) |
| 2b | Deployment | The gateway's process environment (systemd `EnvironmentFile`, shell). Standalone `hubzoid run`: the hub `.env` and the process environment | | see below |
| 2c | Deployment secret | JSON secret | `AWS_SECRET_NAME` in the gateway's environment. Standalone: in the hub `.env` or the environment | see below |
| 3a | Hub | `<hub>/.env` | | that hub's bridge, inbound, Slack and agent runtime |
| 3b | Hub secret | JSON secret | `HUBZOID_HUB_SECRET_NAME` in `<hub>/.env` | same as 3a |
| 4a | Restricted | `<hub>/restricted/.env` | | that hub's restricted tools, in the bridge process |
| 4b | Restricted secret | JSON secret | `HUBZOID_RESTRICTED_SECRET_NAME` in `restricted/.env` | same as 4a |

- **Within a layer, the secret wins over the file.** A key a secret overrides is
  logged by name, never by value.
- **Across layers, the more specific layer wins.** A hub in a gateway applies
  the deployment secret before its own `.env`.
- **A standalone hub treats its `.env` as the deployment layer.** There the
  deployment secret is applied after the `.env` and wins over it. This is the
  familiar `AWS_SECRET_NAME` pattern: load `.env`, then let the secret override.
- **One hub cannot redirect the deployment.** In a gateway, `AWS_SECRET_NAME` in
  a hub `.env` is ignored with a warning. Use `HUBZOID_HUB_SECRET_NAME` for a
  hub's own secret.
- **Secret names are read from their own layer only.** `HUBZOID_HUB_SECRET_NAME`
  is read from `<hub>/.env`. `HUBZOID_RESTRICTED_SECRET_NAME` is read from
  `restricted/.env`.
- `DATABASE_URL`, `DATABASE_SCHEMA` and `HUBZOID_OPERATIONAL_DB` still fail on a
  conflict with the deployment manifest. A hub value of `WEBUI_SECRET_KEY` or an
  `OAUTH_*_ENCRYPTION_KEY` that differs from the deployment's is a startup
  warning, not a failure, because older hub `.env` files carry these keys.

### What each process gets

- **The gateway** reads the deployment secret once at start. It loads hub
  `.env` files only to plan ports and names, and never fetches a hub or
  restricted secret. It records the deployment secret's name and region (never
  a value) in `deployment.json` as `deployment_secret`.
- **Open WebUI** gets the deployment layer without `HUBZOID_*` keys and secret
  names. When Hubzoid read an AWS secret in that process, the AWS credential
  variables are removed too. It never gets a hub or restricted layer. Under
  `hubzoid run` this also stops `restricted/.env` from reaching Open WebUI. An
  Open WebUI or sign-in key found only in a hub secret or `restricted/.env`
  still reaches it, with a warning to move it.
- **Bridges** take only these keys from the deployment secret: the
  `HUBZOID_GATEWAY_ADMIN_*` keys, `WEBUI_SECRET_KEY`, every
  `OAUTH_*_ENCRYPTION_KEY`, `DATABASE_URL`, `DATABASE_SCHEMA`,
  `HUBZOID_OPERATIONAL_DB`, `HUBZOID_PUBLIC_URL`, `WEBUI_URL`, `OWUI_NATIVE_MCP`,
  `HUBZOID_OTEL_ENDPOINT` and `OTEL_*`. The gateway names the other keys at start
  (they stay with the gateway and Open WebUI). A bridge turns the deployment
  `HUBZOID_PUBLIC_URL` into its own `<url>/b/<hub>` address. Each bridge fetches
  its own hub and restricted secrets.
- **Bridges the gateway launches** receive the deployment values from the
  gateway and do not fetch the deployment secret again.
- **External bridges** (`--no-bridges`) find the deployment secret through the
  manifest and fetch it themselves, filtered the same way, before their hub
  layers. Restart them once after the gateway first records the secret.
- **Agent child processes.** The `claude` CLI, and the stdio MCP servers it
  starts, get these variables blanked when present:
  - the secret names: `AWS_SECRET_NAME`, `HUBZOID_HUB_SECRET_NAME`,
    `HUBZOID_RESTRICTED_SECRET_NAME`
  - AWS credential material: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
    `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN`,
    `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, `AWS_CONTAINER_CREDENTIALS_FULL_URI`,
    `AWS_CONTAINER_AUTHORIZATION_TOKEN`, `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`,
    `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_ROLE_ARN`, `AWS_BEARER_TOKEN_BEDROCK`.
    These are kept when `CLAUDE_CODE_USE_BEDROCK` is on, because the CLI then
    signs in with them.
  - Hubzoid and Open WebUI service secrets: `BRIDGE_API_KEYS`,
    `HUBZOID_ARTIFACT_SECRET`, `HUBZOID_GATEWAY_ADMIN_PASSWORD`,
    `WEBUI_SECRET_KEY`, every `OAUTH_*_ENCRYPTION_KEY`, `DATABASE_URL`,
    `HUBZOID_OPERATIONAL_DB`, `HUBZOID_DBOS_DB`
  - every key set by `restricted/.env` or the restricted secret. A key the hub
    layers also set gets the hub layer's value instead of a blank.

  The CLI's own `ANTHROPIC_*` and `CLAUDE_*` keys always pass, so
  `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` keep working. `AWS_PROFILE`,
  `AWS_REGION` and the AWS config file paths pass, because they select
  credentials rather than hold them. Other hub `.env` keys pass unchanged, so a
  stdio MCP server that reads, for example, `SLACK_BOT_TOKEN` still works. An
  MCP server that needs a blanked value can name it in `.mcp.json`
  (`"env": {"TOKEN": "${TOOL_TOKEN}"}`), which the bridge fills in. Restricted
  tools run inside the bridge process for every runtime, so they still see
  their values. The Codex runtime already starts from a short allowlist, and
  the OpenAI runtime's stdio MCP servers get the MCP SDK's minimal environment.
  Agents run as the same OS user as the bridge, so this narrows what children
  inherit. It is not a filesystem sandbox.

### Using AWS Secrets Manager

Store each secret as a flat JSON object of `NAME: value` pairs:

```json
{"WEBUI_SECRET_KEY": "...", "HUBZOID_GATEWAY_ADMIN_PASSWORD": "...", "GOOGLE_CLIENT_SECRET": "..."}
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
  `AWS_SECRET_ACCESS_KEY` (plus `AWS_SESSION_TOKEN` for temporary credentials).
  Hubzoid never passes keys itself.
- **IAM.** Grant `secretsmanager:GetSecretValue` on exactly the named secret
  ARNs, plus `kms:Decrypt` when the secret uses a customer-managed KMS key.
  With one instance role, every process on the host can read every secret that
  role allows. Hubzoid scopes secrets per hub in how it loads them, not in IAM.
  Per-hub IAM isolation needs a task role per hub, or a separate OS user per
  hub.
- **Failure is fatal.** A secret that cannot be read stops the process before
  it serves, with the secret name, the layer and the AWS error class (for
  example `AccessDeniedException` or `NoCredentialsError`). Values never appear
  in messages or logs. Logs show the key count at INFO and the key names at
  DEBUG.

Example gateway environment file:

```bash
AWS_SECRET_NAME=prod/hubzoid/deployment
AWS_REGION=ap-south-1
```

Example hub `.env` and `restricted/.env` lines:

```bash
HUBZOID_HUB_SECRET_NAME=prod/hubzoid/sales            # in sales/.env
HUBZOID_RESTRICTED_SECRET_NAME=prod/hubzoid/sales-tools  # in sales/restricted/.env
```

### Rotation

Values are read once, when a process starts. After rotating a secret, restart:

- the deployment secret: the gateway, then every bridge
- a hub secret: that hub's bridge, inbound and Slack processes
- a restricted secret: that hub's bridge

Rotating `WEBUI_SECRET_KEY` signs everyone out. It also makes the stored
connected-tool tokens (`oauth_session`) undecryptable, so every personal
connection must be made again. Rotate it only on purpose.

## Checking a deployment

`hubzoid doctor <hub>` checks a hub and its deployment without changing
anything: files, configuration layers and secrets, the agent build, schedules,
database schema, bridge keys, chat sign-in, the public bind, model
credentials, backup age and scheduled work. It exits 1 when any check fails.

```bash
hubzoid doctor ./alpha                      # readable
hubzoid doctor ./alpha --json               # for scripts and monitoring
hubzoid doctor ./alpha --skip-secret-fetch  # never call AWS
```

Each check has a stable id such as `auth.bridge_keys`, `db.operational`,
`backup.age` or `scheduler.health`, and a status of `ok`, `info`, `warn` or
`fail`. New checks may be added; existing ids are never renamed.

The layer report (`config.layers`) lists every key a file or secret sets, with
its layer and source. It never shows a value. `secrets.deployment`,
`secrets.hub` and `secrets.restricted` read each named secret once to prove it
is reachable. `secrets.names` warns about a secret name set where it is
ignored. `auth.google_merge` warns when `GOOGLE_CLIENT_ID` is set without
`OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`, which Google sign-in onto an existing
password account needs.

## Path A: native venv on a single Linux box

The walkthrough below uses Ubuntu 24.04 on AWS EC2. The same steps work
on Debian / RHEL-derivatives with the obvious package-manager swaps, and
on any cloud or on-prem VM. Sizing rule of thumb: 2 GB RAM minimum for 1
agent; 4 GB comfortable for 2-3 agents on the same box.

### 1. Server prep

```bash
sudo apt update && sudo apt install -y \
  python3.12 python3.12-venv pkg-config ffmpeg build-essential git curl
sudo useradd -r -m -d /opt/hubzoid -s /bin/bash hubzoid
```

`pkg-config` and `ffmpeg` are the PyAV dependencies that most often bite
a fresh box. Reverse-proxy install comes in step 6 once you've picked
one.

### 2. Firewall / security group

Allow only:

- 22/tcp from your admin IP (SSH)
- 80/tcp from anywhere (most TLS certificate issuers, including Let's
  Encrypt, use it for the HTTP-01 challenge; the proxy also typically
  redirects 80 -> 443)
- 443/tcp from anywhere

Block everything else inbound. Hubzoid's bridge port (8000 by default)
binds to 127.0.0.1, so it is not reachable from outside the box even if
the firewall is permissive.

### 3. Install hubzoid + the agents repo

```bash
sudo -iu hubzoid
git clone git@github.com:your-org/your-hub-agents.git agents
cd agents
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Your agents repo follows the layout described in the
[main README](../README.md): one `requirements.txt` at the parent (pin
to a specific hubzoid version), one folder per hub.

### 4. Configure each hub's `.env`

```bash
cd /opt/hubzoid/agents/devops-agent     # or whatever your hub is named
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Required for production:

- `MODEL=` — either a portable hosted provider with an API key
  (`MODEL=anthropic/claude-haiku-4-5` + `ANTHROPIC_API_KEY`,
  `MODEL=openrouter/...` + `OPENROUTER_API_KEY`, etc.), **or**
  `MODEL=claude-local` to run on your Claude Pro/Max **subscription** with no
  per-token API billing. `claude-local` works in non-interactive prod via a
  long-lived subscription token — no interactive laptop login required. See
  [§5b "Running claude-local in production"](#5b-running-claude-local-in-production-subscription-no-api-key).
- `WEBUI_AUTH=true` plus the auth block from
  [docs/auth.md](auth.md).
- `WEBUI_SECRET_KEY=` set to a random 32-char value (`openssl rand -hex 32`).
  Hubzoid refuses to boot with `WEBUI_AUTH=true` and an unset secret.
- `WEBUI_URL=https://devops.agents.example.com`. Required behind a
  reverse proxy; OAuth callbacks are built from this.
- `PORT=3080`. Unique per hub on the same box.
- `BRIDGE_PORT=8000`. Unique per hub on the same box.

DNS: point `devops.agents.example.com` (A record) at the box's public
IP before starting your reverse proxy. Any auto-issuing proxy (Caddy,
certbot-managed nginx) needs DNS to resolve before it can fetch a TLS
cert.

### 5. systemd unit (one template runs N agents)

Drop the following at `/etc/systemd/system/hubzoid@.service`:

```ini
[Unit]
Description=Hubzoid agent %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hubzoid
Group=hubzoid
WorkingDirectory=/opt/hubzoid/agents
ExecStart=/opt/hubzoid/agents/.venv/bin/hubzoid run %i
# claude-local shells out to the `claude` CLI; the systemd default PATH
# excludes ~/.local/bin, so name it here. Harmless for API-key hubs.
Environment=PATH=/opt/hubzoid/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=10s
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# /opt/hubzoid (the service user's home) must be writable: hubs keep state
# under agents/, and claude-local writes session/cache under ~/.claude.
ReadWritePaths=/opt/hubzoid
# NOTE: do NOT enable ProtectHome here — hiding ~/.claude breaks
# MODEL=claude-local (the CLI can't read its token/config). Left off on purpose.
ProtectKernelTunables=true
ProtectKernelModules=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
```

`%i` is replaced by the systemd instance name. Reload and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hubzoid@devops-agent
sudo systemctl status hubzoid@devops-agent
```

For each additional agent, repeat with the new hub folder's name:
`systemctl enable --now hubzoid@sales-agent`. One unit file, any number of
agents.

Live logs:

```bash
journalctl -u hubzoid@devops-agent -f
```

### 5b. Running claude-local in production (subscription, no API key)

`MODEL=claude-local` runs inference on your Claude Pro/Max **subscription**
instead of a metered API key. It works headless — no interactive laptop
login — via a long-lived subscription token:

1. On any machine logged into your Claude subscription, mint a token:

   ```bash
   claude setup-token        # prints a ~1-year OAuth token (sk-ant-oat01-…)
   ```

   This is **not** an API key (`sk-ant-api03-…`) and is **not** billed
   per-token — usage draws on your subscription's limits. Treat it as a
   secret; rotate by re-running `claude setup-token`.

2. Put it in the hub's `.env` (already `chmod 600`) next to the model:

   ```bash
   MODEL=claude-local
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
   ```

   hubzoid loads `.env` into the environment, and the `claude` CLI it shells
   out to reads `CLAUDE_CODE_OAUTH_TOKEN` automatically. Nothing is passed on
   the command line, and no `claude login` is needed on the box.

3. The shipped `hubzoid@.service` is already claude-local-ready (PATH set,
   `ReadWritePaths=/opt/hubzoid`, no `ProtectHome`). Two prerequisites the
   default hardening would otherwise break, called out so you can verify:

   - The `claude` CLI must be **installed for the service user and on the
     unit's PATH** (the `Environment=PATH=` line in the unit). Install it as
     the `hubzoid` user: `sudo -iu hubzoid` then your usual Claude Code
     install, and confirm `which claude` resolves under `~/.local/bin` or
     `/usr/local/bin`.
   - `claude` writes session/cache under `$HOME/.claude`, so the service
     user's home must be writable (`ReadWritePaths=/opt/hubzoid`) and not
     hidden (`ProtectHome` left unset).

The token lasts ~1 year; re-mint when it expires. Heavy automated use shares
your subscription's rate limits with interactive Claude Code use — if latency
or limits bite, `MODEL=claude-local/haiku` is ~3× faster TTFT, or switch that
hub to a metered API-key provider.

### 6. Reverse proxy + TLS (optional, recommended)

Optional but strongly recommended for any public deployment. **Skip this
section** if the hub is internal-only (behind a VPN, accessed only from
localhost, or reached through an SSH tunnel). Without TLS, chat traffic
is plaintext, Google OAuth refuses to redirect, and modern browsers
strip secure cookies.

Pick whichever reverse proxy you already operate. Three common choices:

| Proxy | Trade-off |
|---|---|
| **Caddy** | One apt package, automatic Let's Encrypt cert issue + renewal, simplest config. Best default if you don't already have a preference. |
| **nginx + certbot** | More configuration, manual cert renewal via certbot cron. Pick this if you already run nginx for other services. |
| **AWS ALB / GCP load balancer / Cloudflare Tunnel** | Managed TLS, no cert work on the box. Higher cost or extra account dependency. Pick this if you already use the platform. |

Whatever you pick, the proxy must:

- Terminate TLS on 443 and redirect 80 to 443.
- Forward to `127.0.0.1:<PORT>` for each hub (the `PORT` from each
  hub's `.env`).
- Pass through Server-Sent Events without buffering (LLM token
  streaming relies on it).
- Carry WebSocket upgrades transparently (most proxies do this by
  default).

Example Caddyfile (most concise of the three, drop in
`/etc/caddy/Caddyfile`):

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

`flush_interval -1` is the SSE-no-buffering knob. `read_timeout 600s`
covers long LLM responses. With Caddy you would `sudo apt install
caddy` then `sudo systemctl reload caddy`; the equivalent in nginx is a
`proxy_buffering off;` directive plus an `Upgrade` header pass-through,
and on ALB it is the default behavior on HTTP/1.1 target groups.

Whichever proxy you use, visit `https://devops.agents.example.com`
after starting it; you should see the OWUI login screen (assuming auth
is on per [docs/auth.md](auth.md)).

### 7. Backup

`hubzoid backup` saves each agent's databases, chat UI data and runtime
state to one archive while chat keeps working. The hub markdown (AGENTS.md,
skills/, knowledge/) is in git and is not included. Drop the following at
`/etc/cron.daily/hubzoid-backup` (`chmod +x`):

```bash
#!/bin/sh
set -e
date=$(date +%F)
cd /opt/hubzoid/agents
for hub in */AGENTS.md; do
  name=$(dirname "$hub")
  .venv/bin/hubzoid backup "$name" --out "/var/backups/hubzoid-${name}-${date}.tar.gz"
done
# Ship offsite, e.g.:
# aws s3 cp /var/backups/ s3://your-bucket/hubzoid/ --recursive --exclude '*' --include "hubzoid-*-${date}.tar.gz"
find /var/backups -name 'hubzoid-*.tar.gz' -mtime +14 -delete
```

Restore: stop the service, `hubzoid restore <archive>`, start. Keep
`WEBUI_SECRET_KEY` stable across restores; changing it signs everyone out.
See [BACKUP.md](BACKUP.md) for what is saved, moving to a new machine and
PostgreSQL.

### 8. Updating hubzoid

OWUI schema migrations have historically not supported rolling updates
across versions. Back up first, then upgrade in place:

```bash
sudo -iu hubzoid
cd /opt/hubzoid/agents
for hub in */AGENTS.md; do .venv/bin/hubzoid backup "$(dirname "$hub")" --out "/tmp/pre-upgrade-$(dirname "$hub")-$(date +%F).tar.gz"; done
# Edit requirements.txt: bump hubzoid==<new-version>
source .venv/bin/activate
pip install -r requirements.txt
exit
sudo systemctl restart 'hubzoid@*'
```

Expect 10-30 seconds of downtime per agent.

### 9. Slack adapter (optional)

The Slack chat surface uses **Socket Mode**, so it does not need a public
URL — no Caddy route, no inbound firewall holes. Two shapes:

**A. Two units (recommended).** Independent restarts. A Slack-side crash
doesn't drop OWUI sessions.

```bash
sudo -iu hubzoid
hubzoid slack systemd /opt/hubzoid/agents/<name> \
  --python /opt/hubzoid/agents/.venv/bin/python \
  --user hubzoid \
  | sudo tee /etc/systemd/system/hubzoid-slack@<name>.service
sudo systemctl daemon-reload
sudo systemctl enable --now hubzoid-slack@<name>.service
```

The unit `Requires=hubzoid@<name>.service`, so the adapter starts only once
the bridge is healthy.

**B. One unit, inline `--slack`.** Simpler if you do not need independent
restarts. Edit `/etc/systemd/system/hubzoid@.service` and append `--slack`:

```ini
ExecStart=/opt/hubzoid/agents/.venv/bin/hubzoid run /opt/hubzoid/agents/%i --slack
```

Then `systemctl daemon-reload && systemctl restart hubzoid@<name>`. A
misconfigured `SLACK_*` token only logs a warning; the bridge + UI stay
up. A Slack-side crash, however, takes the whole unit down — systemd
restarts everything together.

Tokens come from the same `<hub>/.env`. Operator walkthrough (manifest,
app install, troubleshooting) is in [docs/slack.md](slack.md).

### 10. Troubleshooting

| Symptom | Check |
|---|---|
| Anything | Run `hubzoid doctor <hub>` first. |
| Scheduled tasks never run; log says the workflow engine did not start | `hubzoid doctor` `deps.sqlite`: on Python 3.12 the engine needs SQLite 3.42+. Use a Python build with a newer SQLite (python.org, uv, Homebrew, Debian 13, Ubuntu 24.04) or PostgreSQL. |
| `systemctl start` succeeds but the UI is not reachable | `journalctl -u hubzoid@<name> -f` for the OWUI ready line. hubzoid disables Open WebUI's local embedding model, so boot is quick (tens of seconds), not the minutes it would take while fetching that model. |
| Boot fails with `WEBUI_AUTH=true requires WEBUI_SECRET_KEY` | Hubzoid is refusing to start with an unsafe config; set the key in `.env`. |
| Boot fails with `OAuth client IDs are set but WEBUI_URL is not` | Set `WEBUI_URL=https://your.host` in `.env`. |
| TLS certificate never issues | DNS for the hostname is not yet propagated, or the box can't reach the certificate issuer; check your reverse proxy's logs (e.g. `journalctl -u caddy`, `/var/log/nginx/error.log`). |
| Multiple agents conflict on startup | Each hub's `.env` must have a unique `PORT` and `BRIDGE_PORT`. |
| User chats vanish after upgrade | `webui.db` schema migration ran; restore from the pre-upgrade backup and report. |
| Slack adapter loops on restart | `journalctl -u hubzoid-slack@<name>` — usually a missing token or a stale bot token after re-installing the app. See [docs/slack.md](slack.md). |

## Multi-hub on one Open WebUI (`hubzoid gateway`)

`hubzoid run` is one Open WebUI per hub — full isolation, but N heavy OWUI
processes. When you have a hub per team (sales, support, …) on one box, want them
**light**, share branding, and need per-team *access* (not per-team URLs),
run one shared Open WebUI over many headless bridges instead:

```bash
hubzoid gateway sales-agent support-agent finance-agent \
  --host 0.0.0.0 --port 3080 \
  --public-url https://hub.example.com
```

This launches one headless bridge per hub (`hubzoid run <hub> --no-ui`, each
on its hub's `BRIDGE_PORT` — keep them unique), then one Open WebUI connected
to all of them, fronted by the edge router. Each hub becomes a selectable
model. If the bridges already run as their own systemd units, add
`--no-bridges` so the gateway only starts the shared UI — and set
`HUBZOID_OWUI_DB=<data-dir>/webui.db` in each bridge's environment yourself
(the gateway injects it automatically for bridges it launches). SQLite identity
lookups discover the shared path from the deployment manifest, with this
variable as the fallback for unregistered bridges. With PostgreSQL, use the
registered manifest or the same `DATABASE_URL` and `DATABASE_SCHEMA` as the
shared Open WebUI; database lookup does not use the SQLite path. The path also
tells the bridge where OWUI stored uploaded files (they sit in
`<data-dir>/uploads` next to the DB) — without it, chat attachments resolve
against a per-hub dir that never fills in gateway mode and every upload is
reported "unreadable". The gateway forwards the
logged-in user's identity headers to bridges by default (access control
needs them); set `ENABLE_FORWARD_USER_INFO_HEADERS=false` in the gateway's
environment if your external bridges must not receive user emails.

**Where deployment settings live.** Settings for the shared chat app and sign-in
(`WEBUI_AUTH`, `WEBUI_SECRET_KEY`, `WEBUI_URL`, `DEFAULT_USER_ROLE`,
`ENABLE_SIGNUP`, OAuth settings) belong in the gateway's own environment. Each
hub's `.env` is read for that hub only. For deployments upgraded from 0.9.x, the
gateway still takes those sign-in settings from the hub `.env` files when its own
environment does not set them, and lists the keys at start (when hubs disagree,
the last hub listed wins). Nothing else from a hub `.env` reaches the shared
chat app. `WEBUI_NAME` comes from `--name`. To keep these settings in AWS
Secrets Manager instead, see
[Configuration layers and AWS secrets](#configuration-layers-and-aws-secrets).

**One shared access database.** The bridges share `hubzoid-operational.db` in
the data directory (or your `HUBZOID_OPERATIONAL_DB` / PostgreSQL). The gateway
records it in each hub's `.hubzoid/deployment.json` when it starts. With
`--no-bridges`, restart the bridges once after the gateway's first start. Any
bridge can then serve the Console at `/portal/`. The edge asks the next bridge
when one is down or restarting, so restarting one hub does not empty the agent
picker for everyone.

**Auto-provisioning (recommended).** Give the gateway an admin login and it
sets each hub up in Open WebUI by itself, on every boot:

```bash
# in the environment the `hubzoid gateway` process inherits
WEBUI_AUTH=true                 # required — see docs/auth.md for the full block
WEBUI_SECRET_KEY=<openssl rand -hex 32>
HUBZOID_GATEWAY_ADMIN_EMAIL=admin@example.com
HUBZOID_GATEWAY_ADMIN_PASSWORD=<strong password>
```

Provisioning **requires `WEBUI_AUTH=true`** (with auth off, Open WebUI
ignores credentials and would mint its default `admin@localhost` account —
hubzoid refuses to provision in that mode and says so at boot). On a fresh
data dir the configured account is created as the first (admin) user; on an
established gateway the same credentials sign in — a wrong password fails
loudly rather than creating stray accounts. Once Open WebUI is up, the
gateway then creates for every hub:

* its **model entry** — picker name and `description:` from `AGENTS.md`,
  quick-start **`suggestions:`** from `AGENTS.md`, and its avatar from
  `<hub>/branding/logo.png` (raster formats only; SVG is not accepted as an
  avatar) — so each agent looks like itself instead of a bare model id;
* a **team group** named after the hub (its slug), with **read access** to
  that model only.

For new managed hubs, grant people **Use this agent** and tool capabilities in
**Console → Agents → Access**. For an unmigrated hub, its existing team group
continues to apply until the explicit migration in [ADMINISTRATION.md](ADMINISTRATION.md). New hub in the command line → provisioned
on next boot. Provisioning is idempotent and deliberately conservative:
identity fields (name, description, suggestions, avatar) are refreshed from
the hub every boot — including removals, so deleting a `suggestions:` block
or a logo clears it in Open WebUI too — but **access is only seeded when the
model is first created**: ACL changes you make in the UI are never
overwritten. It is also fail-safe: if provisioning can't run (bad
credentials, OWUI hiccup), the gateway logs a warning and boots normally.
Leave both variables unset to skip provisioning entirely.

Every hub must surface as a **unique model id** (from its `AGENTS.md`
`name:` or its `.env` `MODEL_LABEL`) — the gateway refuses to start when two
hubs collide, because they would otherwise share one model entry and one
team's chats could route to the other team's agent.

**Manual setup (no admin credentials).** The same result by hand:

1. Turn on auth (`WEBUI_AUTH=true` + the block from [docs/auth.md](auth.md))
   on the gateway — set these in the environment the `hubzoid gateway`
   process inherits.
2. In **Admin Panel → Users → Groups**, create a group per team (`Sales`,
   `Support`, …) and add members.
3. In **Workspace → Models**, open each agent's model, set **Access Control
   → Private**, and assign its team's group. Users outside the group won't
   see it.

These ACLs live in the shared OWUI database, so they survive restarts
independently of `ENABLE_PERSISTENT_CONFIG`.

**Model access control is forced on.** The gateway keeps
`BYPASS_MODEL_ACCESS_CONTROL=False` so per-team ACLs are enforced — and it
now *forces* that even if `BYPASS_MODEL_ACCESS_CONTROL=True` is in the
inherited environment (that setting is the single-hub fix for the non-admin
empty-model-list problem and must not leak into a gateway, where it would
show every team every other team's agent; the gateway warns when it
overrides). If you truly want an open gateway, set
`HUBZOID_GATEWAY_ALLOW_BYPASS=1`. Single-hub `hubzoid run` is unchanged: it
defaults the bypass to `True`, since a lone hub has one model and nothing to
scope.

**Gateway branding.** The shared chrome (login page, favicon, tab title) is
org-level — one look for the whole gateway. Drop the same files a hub's
`branding/` folder takes (see [docs/branding.md](branding.md)) into
`<data-dir>/branding/` (default `./.hubzoid-gateway/branding/`). Unlike the
single-hub chrome, the gateway keeps the **Workspace** nav visible — that is
where admins manage groups and model access. Per-hub logos appear as each
agent's avatar (from auto-provisioning above), not in the shared chrome.

**Artifact downloads** route per hub: each bridge advertises
`<public-url>/b/<hub-slug>` so its download links come back through the edge
to the right bridge. Leave `HUBZOID_PUBLIC_URL` unset in each hub's `.env`
when using the gateway — `--public-url` injects the per-hub value. (Hard
per-team URL isolation — separate login realms — still needs separate
instances; the gateway shares one login surface by design.)

## Path B: Docker

If `pip install hubzoid` fails on your target OS (PyAV build issues,
Python-version traps, missing system libraries), build the Docker image
from the `Dockerfile` at the repo root and run it instead. It installs the
checked-out source with the reviewed dependency set in `requirements.lock`,
so the image version is the version you checked out. hubzoid disables
Open WebUI's local embedding model, so first boot is fast regardless.

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

The image is a drop-in replacement for `hubzoid run` (it runs
`hubzoid run /hub`; pass other arguments after the image name, e.g.
`hubzoid:<version> run /hub --slack`). State persists in the bind-mounted
hub folder. Publish only port 3080: the edge there serves the chat UI,
artifact downloads, the portal and MCP, while the bridge stays on
127.0.0.1 inside the container. `docker/docker-compose.yml` does the same.
Put a reverse proxy in front of port 3080 the same way Path A does (see
step 6 above). `MODEL=claude-local` does not work inside the image (no
`claude` CLI); use a portable API key.

Each release is also published as a multi-architecture image
(`ghcr.io/hubzoid/hubzoid:<version>`, amd64 and arm64), built by the release
pipeline from the tagged source. Building it yourself gives the same result.

### Docker Compose: SQLite or PostgreSQL

```bash
# SQLite (default): state lives in the mounted hub folder
HUB_PATH=$PWD/my-hub docker compose -f docker/docker-compose.yml up -d

# PostgreSQL: adds a database container; its port is not published
HUB_PATH=$PWD/my-hub POSTGRES_PASSWORD=<letters and digits> \
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.postgres.yml up -d
```

`HUBZOID_IMAGE` picks the image (default `hubzoid:local`, built from this
checkout). Operator commands run inside the container:

```bash
docker compose -f docker/docker-compose.yml exec hubzoid hubzoid doctor /hub
docker compose -f docker/docker-compose.yml exec hubzoid hubzoid backup /hub --out /hub/backup.tar.gz
```

To upgrade, back up, stop, pull or build the new image and start again: the
new version upgrades its tables at start ([UPGRADING.md](UPGRADING.md)).

On Linux, the container runs as an unprivileged user, so the mounted hub folder
must be writable by it (for example `chown -R 999:999 my-hub`, or run with
`--user "$(id -u):$(id -g)"`).

## Path C: ECS, Kubernetes, other orchestrators

The image from Path B is the entry point. Wiring it into your
orchestrator is your responsibility. Two constraints to know before you
start:

- **Run one task / pod per hub, not multiple.** Open WebUI uses SQLite.
  Multiple concurrent writers on the same database produce lock
  corruption. Each agent should be its own service with desired count 1
  and no horizontal autoscaling.
- **SQLite on a network filesystem is not safe.** On ECS Fargate
  specifically, do not put `webui.db` on EFS - file locking semantics
  over NFS will eventually corrupt the DB. The two workable patterns are
  (1) EC2 launch type with an EBS-backed volume, or (2) Fargate with
  `webui.db` on the task's ephemeral storage plus an S3 snapshot
  schedule, mounting EFS only for `uploads/` and `vector_db/`. The same
  caveat applies to any networked filesystem.

Beyond those, the image behaves like a standard FastAPI / Uvicorn
service: it listens on the env-var `PORT` (default 3080), accepts
`.env`-style config as container env vars, and exposes Open WebUI's
`/health` endpoint for liveness probes.

For multi-hub setup, account ownership, migration preview, cutover and rollback,
see [Administration](ADMINISTRATION.md).

## Local Codex runtime

`MODEL=codex-local` is available with the audited Codex CLI 0.147.0. Follow the
[provider setup](providers.md#local-codex) for file-backed login as the service
account, model pinning and isolation details. The standard Docker image does not
include Codex; use a custom image with the pinned CLI and persistent private
service-account credential storage. Do not mount an operator's entire home.
Hubzoid uses temporary per-request configuration and saves refreshed login tokens
back to the configured credential store. Ensure it is writable by the service
account and excluded from hub data, backups shared with users, and source control.
CLI/model upgrades require repeating the real-CLI tool-isolation tests.
