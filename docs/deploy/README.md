# Deploying a hub

A hub you built on your laptop can run where your team can reach it. Every
way of hosting it works the same way:

- **One image.** The released Hubzoid image plus the hub's own files, built
  from its Git repository. The hub's files are only read there, so Settings
  shows them read-only: agent instructions, setting values and budgets.
  Change them in Git and redeploy. Channel changes made in Settings are kept
  in `/data`; `hubzoid channels export` gives the file to commit.
- **One durable folder.** Everything the hub writes goes to `/data`
  (`HUBZOID_DATA_DIR`): accounts, chats, uploads, keys, workflow state and
  backups. SQLite there is the default. PostgreSQL is optional
  (`DATABASE_URL`).
- **Sign-in on and one public address.** `HUBZOID_AUTH=true` and
  `HUBZOID_PUBLIC_URL`, which sign-in, MCP and connector callbacks use.
- **Weekly verified backups**, by default, in `/data/.hubzoid/backups`
  ([BACKUP.md](../BACKUP.md)).

| Where | How | Status |
|---|---|---|
| [Railway](railway.md) | `hubzoid deploy init railway`, then Railway builds the repository's `Dockerfile` with a volume at `/data`. | Not yet verified on Railway |
| [Maritime](maritime.md) | `hubzoid deploy init maritime`, then `maritime create --repo` with its `/data` disk. | Not yet verified on Maritime |
| Docker or Compose on your own server | The image and `docker/docker-compose.yml` ([DEPLOYING.md](../DEPLOYING.md), path B). | Supported |
| A VM with systemd | A virtual environment and a reverse proxy ([DEPLOYING.md](../DEPLOYING.md), path A). | Supported |

## Production checklist

1. The hub is in a Git repository, with no `.env` in it.
2. `hubzoid deploy init <provider>` wrote `Dockerfile`, `.dockerignore` and
   `deploy/<provider>/`. Commit them.
3. A durable volume is mounted at `/data`.
4. The variables from `deploy/<provider>/env.example` are set as the
   provider's secrets, including `HUBZOID_SECRET_KEY`, with a copy kept
   elsewhere.
5. `hubzoid deploy verify https://your-address` passes.

## Verify

`hubzoid deploy verify` checks the deployment from outside, as a person's
browser meets it. Each check prints one line, and the command exits 1 when one
fails:

```text
$ hubzoid deploy verify https://your-hub.up.railway.app
✓ The address uses HTTPS. (https)
✓ /healthz answers {"status": "ok"} and nothing else. (health)
✓ Sign-in is on. (sign_in)
✓ App data needs sign-in (/api/conversations refused a signed-out request). (data)
✓ HUBZOID_PUBLIC_URL is this address. (public_url)
Ready.
```

Then sign in as the owner and ask the agent something. `hubzoid doctor`, run
where the hub runs, checks the rest: where state lives, the model, backups.
