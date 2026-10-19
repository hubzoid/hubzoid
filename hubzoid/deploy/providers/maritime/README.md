# Deploy this hub on Maritime

> **Not yet verified on Maritime.** These steps follow Maritime's documentation
> and run unchanged in local Docker. The live run on a Maritime account has not
> happened yet. Report what you find.

Maritime runs the `Dockerfile` in this folder from your Git repository: the
released Hubzoid image ({{image}}) plus this hub's files. Each agent gets its
own small machine, and its `/data` disk keeps everything the hub writes:
accounts, chats, uploads, keys, workflow state and backups. The hub's own
files come from Git and are read-only in Settings. Change them in Git and
deploy again.

## Before you begin

- The hub in a Git repository, with this folder's `Dockerfile` and
  `.dockerignore` at the repository's root (`hubzoid deploy init maritime`
  wrote them). No `.env` in the repository: `.dockerignore` keeps it out of
  the image anyway.
- A Maritime account and its CLI (`npm install -g maritime-cli`, then
  `maritime login`).
- The model: a Claude subscription token (`claude setup-token`) for
  `MODEL=claude-local`, or an API key.
- Do not run `maritime init` in the hub: it writes its own `AGENTS.md` over
  the hub's.

## Deploy

1. Create the agent from the repository, with a public address:
   `maritime create your-hub --repo https://github.com/you/your-hub --public`.
2. Set the variables from `deploy/maritime/env.example`, one by one
   (`maritime env set your-hub HUBZOID_AUTH=true`) or from a filled copy kept
   outside the repository (`maritime env import your-hub /path/to/hub.env
   --reload`). Values are stored as secrets by default.
3. Deploy again after a change: push, then `maritime deploy your-hub`.

Maritime sets `PORT`, and Hubzoid listens on it.

## Environment variables

| Variable | Why |
|---|---|
| `HUBZOID_PUBLIC_URL` | The exact public address of the agent. Sign-in, MCP and connector callbacks use it. |
| `HUBZOID_AUTH=true` | Sign-in on. A public Maritime address has no login of its own. |
| `HUBZOID_ADMIN_EMAIL`, `HUBZOID_ADMIN_PASSWORD` | The first owner, created at the first start only. Remove the password after you sign in. |
| `HUBZOID_SECRET_KEY` | The key that encrypts stored connections and secrets. Keep a copy outside Maritime. |
| `MODEL` and its credential | `claude-local` with `CLAUDE_CODE_OAUTH_TOKEN`, or an API model with its key. |
| `HUBZOID_BACKUPS` | `weekly` (default), `daily` or `off`. |

SQLite on `/data` is the setup to use here. A PostgreSQL elsewhere
(`DATABASE_URL`) works too, but the disk is still needed for files and keys.

## Persist state and back up

- Only `/data` survives restarts, redeploys and sleep, and everything the hub
  writes is there (`HUBZOID_DATA_DIR`, set in the `Dockerfile`).
- The hub takes a verified backup weekly into `/data/.hubzoid/backups` and
  keeps the last four. `hubzoid doctor` shows the last one.
- Maritime documents no backups of its own. Copy archives elsewhere
  (docs/BACKUP.md, *Copy backups elsewhere*), and keep `HUBZOID_SECRET_KEY`
  apart from them.

## Verify

```bash
hubzoid deploy verify https://your-hub.maritime.sh
```

It checks HTTPS, the health check, that app data needs sign-in and that
`HUBZOID_PUBLIC_URL` matches the address. Then sign in as the owner, open
**Settings > Agents**, and ask the agent something.

## Sleep

An idle machine sleeps and wakes in about a second on the next request.
While it sleeps nothing runs: no schedules, no workflows, no backups. A
backup that fell due runs soon after it wakes. For scheduled work on time,
turn on Always-on for the agent (a paid add-on).

## Troubleshooting

| Seen | Fix |
|---|---|
| `HUBZOID_DATA_DIR=/data is not a folder this process can write` | Check the agent's disk is mounted at `/data`. |
| Sign-in or connector callbacks fail | `HUBZOID_PUBLIC_URL` must be the agent's exact public address, with `https://` and no path. |
| Settings says the agent's files are read-only | Expected: change them in Git and deploy again. |
| `AGENTS.md` was replaced | `maritime init` was run. Restore it from Git. |
| 429 on wake | More machines awake than the plan allows. Stop another, or change plan. |

## Costs and limits

- Free covers 3 machines with 1 awake. Starter is $20 a month (20 machines,
  5 awake), and Always-on is extra. Check Maritime's pricing page.
- Each machine has 1 vCPU, 2 GB of memory and a 5 GB disk, which can grow.
- A Claude subscription token is for your own use. Check its terms before you
  share a hosted hub with a team.
