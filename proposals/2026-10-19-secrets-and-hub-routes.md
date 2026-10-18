# Secrets in the Console, one secret reference grammar, and hub routes

Status: proposed for 2.0. Covers GitHub #49 (write-only secret store), with the
rules from #48 and #47 that it builds on.

## Problem

Every secret is an environment variable today. It lives in the process
environment, `<hub>/.env`, `<hub>/restricted/.env`, or an AWS Secrets Manager
secret named from one of those. To set or rotate a key an operator needs a shell
on the server, an edit to the right file and a restart.

That causes real problems:

- On hosted providers (Railway, Maritime) there is no shell. A gateway's
  per-agent key can only go in that hub's `.env` on disk, which the provider's
  variables screen cannot reach.
- Teams running several hubs copy shared keys into every hub `.env`. The copies
  drift, and one blank copy fails silently. Nothing reports a key that is
  present but empty, or a hub file that overrides a deployment value.
- Hubzoid itself will create secrets that no operator types. A GitHub App
  private key from a manifest flow is one example. These have nowhere to live
  except a file.
- Features that need a credential each describe it differently. There is no
  single way to say "this setting is a secret, find it here".

## Why now

2.0 adds hosted deployments, channel connections and unattended model routes.
Each needs a secret that someone sets from a browser or that Hubzoid creates
itself. Without one store and one reference grammar, each feature would invent
its own table.

## What should happen

**One write-only store.**
- Values are encrypted with the existing deployment key (`hubzoid.secretbox`,
  which already encrypts connector secrets) and kept in the operational
  database.
- A value is set, replaced or cleared, never read back. The Console, the CLI
  and the API show "Set", when and by whom, and never any part of the value.

**The existing levels stay.**
- Deployment, hub and restricted, each with its optional AWS secret, resolve
  exactly as today.
- They are operator sources. A key an operator source sets wins, and shows as
  locked with its source ("Set in hub .env. Remove the line to manage it
  here."). The API refuses to change it.
- A stored value that an operator source later overrides is kept but unused,
  and `hubzoid doctor` says so.
- Nothing writes `.env` files or AWS secrets.

**Applies after a restart.**
- Stored secrets are decrypted once, after the database is migrated and before
  the bridge builds its runtime. They are never changed while it serves.
- A secret stored as restricted joins the restricted layer, so the `claude`
  child process and stdio MCP servers get it blank, exactly as with
  `restricted/.env`.

**Who.**
- Organization administrators set and clear secrets.
- An agent's administrators see that agent's secret names and status, and a
  count of its restricted keys, but not their names.
- Every change writes an audit row with key, scope, actor and action, never a
  value.

**Not for bootstrap secrets.** Database URLs, the deployment key, bridge keys,
sign-in client secrets, `HUBZOID_*` configuration and process-control
variables stay operator-only.

**Bound to its destination.** A stored Azure key records its endpoint and API
version. If either changes from any source, the key is not used and its row
asks for it again, so a stored key cannot be redirected silently.

**One reference grammar.**
- A file that needs a credential names it, never holds it:
  - `secret:deployment/<name>`
  - `secret:hub/<hub_id>/<name>`
  - `secret:channel/<hub_id>/<channel_id>/<name>`
  - `env:<NAME>`
- In YAML, the key ends in `_ref`.
- `env:` resolves through the existing layers. `secret:` reads the store.

**Commands and routes.**
- `hubzoid secrets list|set|delete [HUB]` lists every key with its source and
  lock, without values. Values are read hidden from the terminal or from
  `--stdin`, never from an argument.
- The same operations are `GET`, `PUT` and `DELETE` on
  `/api/hubs/{hub_id}/secrets[/{name}]`, and on `/api/secrets[/{name}]` for
  the deployment.
- The Console gets a Secrets tab for each agent.

**Doctor.** Reports:
- keys that are missing or blank;
- stored keys that are overridden or cannot be decrypted (for example after a
  restore with a different deployment key);
- hub files that override a deployment value.

**Hub routes.** A gateway's edge sends `/api/hubs/{hub_id}/*` to that hub's
bridge by hub key, alongside today's `/b/<slug>/*`. A bridge answers only for
its own hub ID and returns 404 `hub_not_found` for any other. Hub-scoped APIs,
starting with secrets, live there.

## Scope and non-goals

**Not in this change:**
- Editing non-secret settings in the Console (#47, #50, #51).
- Channel tokens (#52). The store already supports the channel scope.
- Restarting a bridge from the Console.
- Importing values from `.env` files.

**No new dependency.** python-dotenv, boto3 and cryptography are already in use.

## Open questions

Do we need a Console restart action once more settings become editable? That
belongs with #47.

## Definition of done

- No API response, log line, audit row or error message contains a stored
  value (tested with known values).
- A provider key stored in the Console is used after the bridge restarts.
- A restricted stored secret reaches its restricted tool and is blank in the
  `claude` child and stdio MCP children.
- A key also set in `.env` is locked in the Console and refused by the API.
- Changing `AZURE_API_BASE` after storing `AZURE_API_KEY` stops the key being
  used, and its row asks for it again.
- A backup restored with the same deployment key brings the secrets back. With
  a different key they show "Cannot be read: set again".
- Agent administrators cannot set, clear or list restricted key names, and the
  server refuses.
- In a two-hub gateway, `/api/hubs/<a>/secrets` reaches hub a's bridge, and
  hub b's bridge answers 404 `hub_not_found` for hub a.
