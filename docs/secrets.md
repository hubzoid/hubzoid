# Secrets

Hubzoid keeps each secret in one place, by kind. Keys a person types live in
`.env` files and other operator sources. Keys Hubzoid obtains itself through a
setup or sign-in flow live in an encrypted secret store in the operational
database. Values are write-only: no screen, command or API ever shows one.

```bash
hubzoid secrets list .
printf '%s' "$TOKEN" | hubzoid secrets set . --name ERP_TOKEN --restricted --stdin
hubzoid restart <hub>          # a bridge reads stored secrets when it starts
```

## Where each secret lives

| Kind | Examples | Where |
|---|---|---|
| Keys a person types | Model keys, tool tokens, sign-in client secrets | Operator sources only: the process environment, `<hub>/.env`, `<hub>/restricted/.env`, the deployment's `.env`, and the optional AWS Secrets Manager secret each of those can name. See [configuration layers](DEPLOYING.md#configuration-layers-and-aws-secrets). |
| Keys Hubzoid obtains itself | A GitHub App key from the manifest flow, Linear and mailbox OAuth tokens | The secret store, encrypted with the deployment key |
| Channel secrets | A channel file's `secret:channel/<hub>/<id>/<name>` | The secret store |

Settings, the API and `hubzoid secrets set` refuse to store a key of the
first kind (`operator_source`) and say which `.env` to use.

A key typed into the store before this rule is not used (status
`move_to_env`), so removing a `.env` line never brings an old value back.
`hubzoid upgrade` moves it to the right `.env`.

A line with an empty value (`OPENAI_API_KEY=`, often copied from
`.env.example`) sets nothing. A stored value fills it, and doctor warns about
the empty line.

## Levels

| Level | Who uses it | Where to set it |
|---|---|---|
| Shared by all agents | Every hub of the deployment | `hubzoid secrets set HUB --name NAME --deployment`, `PUT /api/secrets/{name}` |
| This agent | This hub, its tools and its model | `hubzoid secrets set HUB --name NAME`, `PUT /api/hubs/{hub_id}/secrets/{name}` |
| Restricted tools only | This hub's `restricted/` tools | `hubzoid secrets set HUB --name NAME --restricted` |

A hub value wins over a shared one with the same name. A restricted value is
blank for the `claude` process and the MCP servers it starts, like
`restricted/.env`.

## The deployment key

The store is encrypted with the deployment key: `HUBZOID_SECRET_KEY`, or else
a `secret.key` file Hubzoid creates on first use (next to the deployment's
manifest, or `<hub>/.hubzoid/secret.key` for a standalone hub).

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_SECRET_KEY` | unset (use the key file) | One or more keys separated by commas. The first encrypts and signs. Every key decrypts, so a new key can be put first while the old one still reads existing values. |
| `HUBZOID_REQUIRE_SECRET_KEY` | off | `true` refuses to start without `HUBZOID_SECRET_KEY` instead of creating a key file. Use it when several services or containers must share one key. |

```bash
hubzoid secrets new-key        # prints a new key for your secret store
```

Every service of a deployment uses the same key. Back it up separately from
the data. Without it, stored secrets and connector tokens cannot be read.

## Manage secrets from the command line

```bash
hubzoid secrets list .
hubzoid secrets set . --name OPENAI_API_KEY
printf '%s' "$TOKEN" | hubzoid secrets set . --name ERP_TOKEN --restricted --stdin
hubzoid secrets delete . --name OPENAI_API_KEY
```

| Option | Commands | What it does |
|---|---|---|
| `--name` | `set`, `delete` | The secret's name |
| `--ref` | `set`, `delete` | A reference instead of a name, such as a channel's `secret:channel/...` |
| `--deployment` | `set`, `delete` | Shared by every hub of the deployment |
| `--restricted` | `set` | For this hub's restricted tools only |
| `--stdin` | `set` | Read the value from standard input |
| `--check` | `set`, `delete` | Show what would change. Writes nothing. |
| `--expected-revision N` | `set`, `delete` | Change only that revision, or `absent` to create only |
| `--skip-secret-fetch` | `list` | Do not read AWS secrets |
| `--json` | all | Machine-readable output |

The value is asked for hidden, or read from standard input with `--stdin`. It
is never taken from an argument.

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | Failed |
| 2 | Invalid input |
| 3 | Not allowed |
| 4 | Conflict: a changed revision, or a name an operator source sets |
| 5 | The store is unavailable |

## Manage secrets in Settings

The agent's **Configuration** tab, **Secrets**, lists:

- every secret a source sets
- every stored secret
- the names the hub needs: its model's API key, a secret for each declared
  webhook, every name in `.env.example` that looks like a key, token or
  password, and every name in `restricted/.env.example`

Listings show the name, where it is used, its status, its source, and who
saved it and when. The audit log records the name, scope and actor of each
change, never the value.

Names only an operator sets are never offered: database URLs, the deployment
key, bridge keys, sign-in client secrets, `HUBZOID_*` settings and AWS
credentials. Nor are names that steer the bridge's own programs or where its
keys go, because a stored name joins the environment every subprocess
inherits: `GIT_*`, `SSH_*`, `BASH_ENV`, `PYTHON*`, `NODE_*`, `CLAUDE_CODE_*`
(except the `CLAUDE_CODE_OAUTH_TOKEN` names), proxies, CA bundles and the
provider base URLs (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`,
`OPENAI_API_BASE`, `OPENROUTER_API_BASE`). One stored before this rule is
left out at start and shown as overridden.

### Who can do what

- Organization administrators set and remove secrets, and see every name.
- An agent's administrators see that agent's names and status, and a count of
  its restricted secrets, but not their names. They cannot change secrets.
- When sign-in is off, the local owner is the administrator. Only a request
  from this machine is the local owner.

## Apply a change

A bridge reads stored secrets once, when it starts, and never changes them
while it serves. After changing a secret, restart the hub
(`hubzoid restart <hub>`, or redeploy). Until then Settings marks the row
**Restart to apply**.

## Bound values

`AZURE_API_KEY` is saved with the Azure endpoint and API version it is for.
If either changes, from any source, the key is not used and its row says
**Set again for this destination**, so a key cannot be redirected silently.

## Refer to a secret from a file

A file that needs a credential names it with a reference, never the value. In
YAML the key ends in `_ref`:

```yaml
client_secret_ref: secret:deployment/google-oauth
token_ref: secret:hub/support-hub/ZOHO_REFRESH_TOKEN
password_ref: env:SMTP_PASSWORD
```

| Reference | Reads |
|---|---|
| `secret:deployment/<name>` | The deployment's store |
| `secret:hub/<hub_id>/<name>` | One hub's store (only that hub may read it) |
| `secret:channel/<hub_id>/<channel_id>/<name>` | One channel's slot |
| `env:<NAME>` | The layered configuration |

Code that needs the value calls `hubzoid.secretstore.resolve(ref, hub_dir)`.

## Hub-scoped API routes

Hub-scoped routes live under `/api/hubs/{hub_id}/...`, where the id is the hub
key (the lowercase hub folder name, as access grants use it). In a deployment
of several hubs the edge sends `/api/hubs/<key>/*` to that hub's bridge,
alongside `/b/<slug>/*`. A bridge answers only for its own hub and returns 404
`hub_not_found` for any other id.

## Backups

Stored secrets are in the operational database, so a backup keeps them. They
decrypt only with the same deployment key (`HUBZOID_SECRET_KEY` or
`secret.key`). See [Backup and restore](BACKUP.md).

## Troubleshooting

`hubzoid doctor` reports, by name only:

- stored secrets that are unused, because an operator source sets them
- stored secrets that cannot be read (for example after a restore with a
  different deployment key) or are bound to a changed destination
- names the hub needs that nothing sets
- empty values in `.env` files
- hub files that set a different value than the deployment, so shared values
  stay in one place

| Symptom | Check |
|---|---|
| A row says **Cannot be read: set again** | The data was restored with a different deployment key. Restore the original key, or set the value again. |
| A row says **Restart to apply** | Restart the hub. |
| A row says **Set again for this destination** | The Azure endpoint or API version changed. Set the key again. |
| `set` exits 4 naming an operator source | An `.env` or AWS secret already sets this name. Change it there. |
| Start stops: `HUBZOID_REQUIRE_SECRET_KEY is on and HUBZOID_SECRET_KEY is not set` | Set `HUBZOID_SECRET_KEY` to the deployment's key, or make one with `hubzoid secrets new-key` for a new deployment. |
