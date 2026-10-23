# Secrets

Each secret has one home, by kind:

- **Keys a person types** (model keys, tool tokens, sign-in client secrets)
  live in operator sources only: the process environment, `<hub>/.env`,
  `<hub>/restricted/.env`, the deployment's `.env`, and the optional AWS Secrets
  Manager secret each of those can name. See [Deploying](DEPLOYING.md) for the
  layers. Settings, the API and `hubzoid secrets set` refuse to store them
  (`operator_source`) and say which `.env` to use.
- **Keys Hubzoid obtains itself** through a setup or sign-in flow (a GitHub App
  key from the manifest flow, Linear and mailbox OAuth tokens) and secrets a
  channel file names as `secret:channel/<hub>/<id>/<name>` live in the secret
  store, encrypted with the deployment key in the operational database.

A key typed into the store before this rule is not used (status
`move_to_env`), so removing a `.env` line never brings an old value back;
`hubzoid upgrade` moves it to the right `.env`.

A line with an empty value (`OPENAI_API_KEY=`, often copied from
`.env.example`) sets nothing, so a stored value fills it, and doctor warns about
the empty line.

## Write-only

A stored value is never shown or returned, by Settings, the CLI or the
API. Listings show the name, where it is used, its status, its source, and who
saved it and when. The audit log records the name, scope and actor of each
change, never the value.

## When a value applies

A bridge reads stored secrets once, when it starts, and never changes them
while it serves. After changing a secret, restart the hub
(`hubzoid restart <hub>`, or redeploy). Until then Settings marks the row
**Restart to apply**.

## Levels

| Level | Who uses it | Where to set it |
|---|---|---|
| Shared by all agents | Every hub of the deployment | `hubzoid secrets set HUB --name NAME --deployment`, `PUT /api/secrets/{name}` |
| This agent | This hub, its tools and its model | `hubzoid secrets set HUB --name NAME`, `PUT /api/hubs/{hub_id}/secrets/{name}` |
| Restricted tools only | This hub's `restricted/` tools | `hubzoid secrets set HUB --name NAME --restricted` |

A hub value wins over a shared one with the same name. A restricted value is
blank for the `claude` process and the MCP servers it starts, exactly like
`restricted/.env`.

## Who can do what

- Organization administrators set and remove secrets, and see every name.
- An agent's administrators see that agent's names and status, and a count of
  its restricted secrets, but not their names. They cannot change secrets.
- When sign-in is off, the local owner on this machine is the administrator.

## Commands

```bash
hubzoid secrets list .
hubzoid secrets set . --name OPENAI_API_KEY
printf '%s' "$TOKEN" | hubzoid secrets set . --name ERP_TOKEN --restricted --stdin
hubzoid secrets delete . --name OPENAI_API_KEY
```

The value is asked for hidden, or read from standard input with `--stdin`. It
is never taken from an argument. Every command takes `--json`. `set` and
`delete` take `--check` (show what would change, write nothing) and
`--expected-revision N` (change only that revision, or `absent` to create
only). Exit codes: 0 done, 1 failed, 2 invalid input, 4 conflict (a changed
revision, or a name an operator source sets), 5 the store is unavailable.

## What Settings offers

The agent's **Configuration** tab, **Secrets**, lists:

- every secret a source sets;
- every stored secret;
- the names the hub needs:
  - its model's API key;
  - a secret for each declared webhook;
  - every name in `.env.example` that looks like a key, token or password;
  - every name in `restricted/.env.example`.

Names an operator alone sets are never offered: database URLs, the deployment
key, bridge keys, sign-in client secrets, `HUBZOID_*` settings and AWS
credentials. Nor are names that steer the bridge's own programs or where its
keys go, because a stored name joins the environment every subprocess
inherits: `GIT_*`, `SSH_*`, `BASH_ENV`, `PYTHON*`, `NODE_*`, `CLAUDE_CODE_*`
(except the `CLAUDE_CODE_OAUTH_TOKEN` names), proxies, CA bundles and the
provider base URLs (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`,
`OPENAI_API_BASE`, `OPENROUTER_API_BASE`). One stored before this rule is
left out at start and shown as overridden.

## Bound values

`AZURE_API_KEY` is saved with the Azure endpoint and API version it is for.
If either changes, from any source, the key is not used and its row says
**Set again for this destination**, so a key cannot be redirected silently.

## References

A file that needs a credential names it with a reference, never the value. In
YAML the key ends in `_ref`:

```yaml
client_secret_ref: secret:deployment/google-oauth
token_ref: secret:hub/support-hub/ZOHO_REFRESH_TOKEN
password_ref: env:SMTP_PASSWORD
```

| Reference | Reads |
|---|---|
| `secret:deployment/<name>` | the deployment's store |
| `secret:hub/<hub_id>/<name>` | one hub's store (only that hub may read it) |
| `secret:channel/<hub_id>/<channel_id>/<name>` | one channel's slot |
| `env:<NAME>` | the layered configuration |

Code that needs the value calls `hubzoid.secretstore.resolve(ref, hub_dir)`.

## Doctor

`hubzoid doctor` reports, by name only:

- stored secrets that are unused, because an operator source sets them;
- stored secrets that cannot be read (for example after a restore with a
  different deployment key) or are bound to a changed destination;
- names the hub needs that nothing sets;
- empty values in `.env` files;
- hub files that set a different value than the deployment, so shared values
  stay in one place.

## Backups

Stored secrets are in the operational database, so a backup keeps them. They
decrypt only with the same deployment key (`HUBZOID_SECRET_KEY` or
`secret.key`). Restored with a different key, they show **Cannot be read: set
again**. See [Backup and restore](BACKUP.md).

## Hub-scoped API routes

Hub-scoped routes live under `/api/hubs/{hub_id}/...`, where the ID is the hub
key (the lowercase hub folder name, as access grants use it). In a gateway the
edge sends `/api/hubs/<key>/*` to that hub's bridge, alongside `/b/<slug>/*`.
A bridge answers only for its own hub and returns 404 `hub_not_found` for any
other ID.
