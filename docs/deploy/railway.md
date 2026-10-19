<!-- Generated from hubzoid/deploy/providers/railway/README.md: `hubzoid deploy init railway` writes this page into a hub as deploy/railway/README.md. Edit the template, not this file. -->

# Deploy this hub on Railway

> **Not yet verified on Railway.** These steps follow Railway's documentation
> and run unchanged in local Docker. The live run on a Railway account has not
> happened yet. Report what you find.

Railway builds the `Dockerfile` in this folder from your Git repository: the
released Hubzoid image (ghcr.io/hubzoid/hubzoid:<version>) plus this hub's files. One volume at
`/data` keeps everything the hub writes: accounts, chats, uploads, keys,
workflow state and backups. The hub's own files come from Git and are
read-only in Settings. Change them in Git and Railway redeploys.

## Before you begin

- The hub in a Git repository on GitHub, with this folder's `Dockerfile` and
  `.dockerignore` at the hub's root (`hubzoid deploy init railway` wrote them).
  No `.env` in the repository: `.dockerignore` keeps it out of the image anyway.
- A Railway account, and the [Railway CLI](https://docs.railway.com/guides/cli)
  if you prefer it to the dashboard.
- The model: a Claude subscription token (`claude setup-token`) for
  `MODEL=claude-local`, or an API key.

## Deploy

1. Create a project from the repository: in the dashboard, **New project >
   Deploy from GitHub repo**, or `railway init` in this folder. If the hub is
   a subfolder of the repository, set the service's **Root directory** to it.
2. Add the volume: `railway volume add --mount-path /data`, or **Add volume**
   on the service with mount path `/data`.
3. Give the service a public domain (**Settings > Networking > Generate
   domain**) and note it.
4. Set the variables from `deploy/railway/env.example`
   (**Variables > Raw editor**). Typing secrets into the dashboard keeps them
   out of your shell history.
5. Deploy: push to the repository, or `railway up`.

Railway sets `PORT`, and Hubzoid listens on it. There is no `railway.json`
because Railway stops reading those files on 1 December 2026.

## Environment variables

| Variable | Why |
|---|---|
| `HUBZOID_PUBLIC_URL` | The exact address people open. Sign-in, MCP and connector callbacks use it. |
| `HUBZOID_AUTH=true` | Sign-in on. A public address without it is refused at start. |
| `HUBZOID_ADMIN_EMAIL`, `HUBZOID_ADMIN_PASSWORD` | The first owner, created at the first start only. Remove the password after you sign in. |
| `HUBZOID_SECRET_KEY` | The key that encrypts stored connections and secrets. Keep a copy outside Railway. |
| `RAILWAY_RUN_UID=0` | Railway mounts volumes as root. Without it the hub cannot write `/data`. |
| `MODEL` and its credential | `claude-local` with `CLAUDE_CODE_OAUTH_TOKEN`, or an API model with its key. |
| `DATABASE_URL` (optional) | PostgreSQL instead of SQLite. Add Railway PostgreSQL and use `${{Postgres.DATABASE_URL}}`. The volume is still needed for files and keys. |
| `HUBZOID_BACKUPS` | `weekly` (default), `daily` or `off`. |

## Persist state and back up

- Everything the hub writes is under `/data` (`HUBZOID_DATA_DIR`, set in the
  `Dockerfile`). It survives redeploys and restarts. Without the volume the
  hub starts empty on every deploy.
- The hub takes a verified backup weekly into `/data/.hubzoid/backups` and
  keeps the last four. `hubzoid doctor` shows the last one.
- Railway's own volume backups, on plans that offer them, stay inside
  Railway. Copy archives elsewhere as well (docs/BACKUP.md, *Copy backups
  elsewhere*). Keep `HUBZOID_SECRET_KEY` apart from the archives.
- A Railway volume serves one service and no replicas. A redeploy has a brief
  pause.

## Verify

```bash
hubzoid deploy verify https://your-hub.up.railway.app
```

It checks HTTPS, the health check, that app data needs sign-in and that
`HUBZOID_PUBLIC_URL` matches the address. Then sign in as the owner, open
**Settings > Agents**, and ask the agent something.

## Sleep

Railway's Serverless (app sleeping) stops the service after a few minutes
without traffic and wakes it on the next request, with a delay or an error
page for the first one. While it sleeps nothing runs: no schedules, no
workflows, no backups. A backup that fell due runs soon after it wakes. Leave
sleeping off when the hub runs scheduled work.

## Troubleshooting

| Seen | Fix |
|---|---|
| 502 or "Application failed to respond" | The service is still starting, or `PORT` was overridden. Leave `PORT` unset. |
| `HUBZOID_DATA_DIR=/data is not a folder this process can write` | Add the volume at `/data`, and set `RAILWAY_RUN_UID=0`. |
| Sign-in or connector callbacks fail | `HUBZOID_PUBLIC_URL` must be the exact domain, with `https://` and no path. |
| Settings says the agent's files are read-only | Expected: change them in Git and redeploy. |
| The hub starts empty after a deploy | The volume is missing or mounted somewhere other than `/data`. |

## Costs and limits

- Hobby is $5 a month, including $5 of usage. Pro is $20 a month, including
  $20. A small hub mostly uses memory. Check Railway's pricing page.
- Volumes cost about $0.15 per GB-month. Volume size limits depend on the
  plan.
- A Claude subscription token is for your own use. Check its terms before
  you share a hosted hub with a team.
