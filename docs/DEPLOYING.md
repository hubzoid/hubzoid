# Deploying Hubzoid to production

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

- `MODEL=` set to a portable provider with an API key. `MODEL=claude-local`
  works only on a logged-in developer laptop; it does not work in
  non-interactive prod.
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
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/hubzoid/agents
ProtectHome=true
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
`systemctl enable --now hubzoid@irs-agent`. One unit file, any number of
agents.

Live logs:

```bash
journalctl -u hubzoid@devops-agent -f
```

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

irs.agents.example.com {
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

Per agent, back up `<hub>/.openwebui-data/` nightly. It holds the SQLite
user database, uploads, and OWUI cache. The hub markdown (AGENTS.md,
skills/, knowledge/) is in git and does not need to be backed up
separately.

Drop the following at `/etc/cron.daily/hubzoid-backup` (`chmod +x`):

```bash
#!/bin/sh
set -e
date=$(date +%F)
cd /opt/hubzoid/agents
tar czf /var/backups/hubzoid-${date}.tgz */.openwebui-data
# Ship offsite, e.g.:
# aws s3 cp /var/backups/hubzoid-${date}.tgz s3://your-bucket/hubzoid/
find /var/backups -name 'hubzoid-*.tgz' -mtime +14 -delete
```

Restore = stop the service, replace `.openwebui-data/` with the
unpacked backup, start. Keep `WEBUI_SECRET_KEY` stable across restores;
changing it invalidates all existing sessions.

### 8. Updating hubzoid

OWUI schema migrations have historically not supported rolling updates
across versions. Back up first, then upgrade in place:

```bash
sudo -iu hubzoid
cd /opt/hubzoid/agents
tar czf /tmp/pre-upgrade-$(date +%F).tgz */.openwebui-data
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
| `systemctl start` succeeds but the UI is not reachable | `journalctl -u hubzoid@<name> -f` for the OWUI ready line. First boot can take 1-2 min while the embedding model is fetched. |
| Boot fails with `WEBUI_AUTH=true requires WEBUI_SECRET_KEY` | Hubzoid is refusing to start with an unsafe config; set the key in `.env`. |
| Boot fails with `OAuth client IDs are set but WEBUI_URL is not` | Set `WEBUI_URL=https://your.host` in `.env`. |
| TLS certificate never issues | DNS for the hostname is not yet propagated, or the box can't reach the certificate issuer; check your reverse proxy's logs (e.g. `journalctl -u caddy`, `/var/log/nginx/error.log`). |
| Multiple agents conflict on startup | Each hub's `.env` must have a unique `PORT` and `BRIDGE_PORT`. |
| User chats vanish after upgrade | `webui.db` schema migration ran; restore from the pre-upgrade backup and report. |
| Slack adapter loops on restart | `journalctl -u hubzoid-slack@<name>` — usually a missing token or a stale bot token after re-installing the app. See [docs/slack.md](slack.md). |

## Path B: Docker

If `pip install hubzoid` fails on your target OS (PyAV build issues,
Python-version traps, missing system libraries), build the Docker image
from the `Dockerfile` at the repo root and run it instead. The image
pre-bakes the Open WebUI embedding model so first boot is fast.

```bash
docker build --build-arg HUBZOID_VERSION=0.4.0 -t hubzoid:0.4.0 .

docker run -d --restart unless-stopped \
  --name devops-agent \
  -p 3080:3080 \
  -v "$PWD/devops-agent:/hub" \
  --env-file "$PWD/devops-agent/.env" \
  hubzoid:0.4.0
```

The image is a drop-in replacement for `hubzoid run`. State persists in
the bind-mounted hub folder. Put a reverse proxy in front of port 3080
the same way Path A does (see step 6 above). `MODEL=claude-local` does
not work inside the image (no `claude` CLI); use a portable API key.

Hubzoid does not publish a prebuilt image. Build it yourself from the
`Dockerfile`; it's a few minutes one-time and stays under your control.

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
