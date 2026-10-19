# Authentication

Hubzoid signs people in with its own accounts. Sign-in is off by default:
`hubzoid run` starts in **local mode**, where you are the hub's owner and the
web app stays on your machine. Turn sign-in on before anyone else can reach
the hub.

| Mode | Use it for |
|---|---|
| Local mode (`HUBZOID_AUTH` unset or false, the default) | Trying a hub on your own machine |
| Sign-in on (`HUBZOID_AUTH=true`) | Anything other people can reach: a team, a server, a container |

With sign-in on, people sign in with a password, Google, Microsoft or one
standard OpenID Connect provider (Okta, Auth0, Keycloak, authentik and
others). One account works for the web app and its Settings, hosted MCP and
every agent of a [gateway](DEPLOYING.md). An account is not access: what
someone may use is granted per agent in Settings (see
[administration](ADMINISTRATION.md)).

Hubzoid 2.0 has no Open WebUI sign-in. A deployment moving from it renames its
settings as described in [upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed).

## Local mode

Nothing to set. Every request that addresses this machine is the **local
owner**, `admin@localhost`, an administrator. That covers `localhost` and other
`.localhost` names, IP addresses, the address the server listens on, and the
addresses in `HUBZOID_PUBLIC_URL` and `HUBZOID_ALLOWED_ORIGINS`. A request
that uses any other host name is not treated as the owner, which stops another
web page from reaching your hub through DNS rebinding. If you open the hub
through a tunnel or a LAN name, list that address in `HUBZOID_PUBLIC_URL` or
`HUBZOID_ALLOWED_ORIGINS`.

Local mode keeps the public port on loopback. `hubzoid run --host 0.0.0.0`
(or `HUBZOID_HOST`) without sign-in stops with exit code 2 and explains how to
turn sign-in on. On a network you trust you can accept the risk with
`HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`: then anyone who can reach the
port uses the hub as its owner.

The local owner never signs in with a password, a link or an external
provider, including after you turn sign-in on.

## Turn sign-in on

In the hub's `.env` (for a gateway, in the gateway's environment):

```dotenv
HUBZOID_AUTH=true
HUBZOID_PUBLIC_URL=https://hub.example.com   # the address people open
```

`HUBZOID_PUBLIC_URL` is needed when people reach Hubzoid through a proxy or a
name other than `localhost`, and for Google, Microsoft and OpenID Connect
sign-in. The Open WebUI names `WEBUI_AUTH` and `WEBUI_URL` are no longer read:
a start that finds either stops and asks you to rename it, so sign-in is never
turned off by a dropped name.

Then create the first administrator. Either:

- **With the admin command** (works on any hub, including one you already ran
  in local mode):

  ```bash
  hubzoid admin create you@example.com my-hub --owner
  ```

  `--owner` makes you an Administrator with the owner's access on every hub of
  the deployment. The command prints a one-time sign-in link (open it, set a
  password, and you are signed in). Use `--password` to type a password at a
  hidden prompt instead.

- **From the environment**, for a deployment that has never run (a new server
  or container):

  ```dotenv
  HUBZOID_ADMIN_EMAIL=you@example.com
  HUBZOID_ADMIN_PASSWORD=<at least 8 characters>
  # HUBZOID_ADMIN_NAME=Your Name      # optional
  ```

  When sign-in is on and no account exists yet (the local owner does not
  count), the first start creates this administrator and gives it the owner's
  access. Remove both lines after the first start. The Open WebUI names
  `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD` stop the start until renamed.

  The owner's access is given once per hub. A hub you already ran in local
  mode has the local owner as its owner, so this route creates an
  Administrator account with no access there. Use `hubzoid admin create
  --owner` instead, or fix it afterwards with `hubzoid admin set-role
  you@example.com admin my-hub` and give yourself **Use this agent** in
  Settings.

Restart the hub. `hubzoid run` prints `(sign-in on)` in its ready line.
Teammates are added in Settings (**People & access → Add user**) or with `hubzoid
admin create`. Nothing is emailed: you share each person's sign-in link
yourself.

## Passwords

- New passwords are hashed with Argon2id. They are 8 to 1024 characters.
- Passwords moved from Open WebUI (bcrypt) keep working and are rehashed with
  Argon2id at the person's next sign-in.
- People change their own password on the **Account** page. That needs their
  current password and ends their other sessions. A change is refused when an
  administrator resets the password, changes the role or blocks the person
  while it is being made, so it never undoes the administrator's action.
- An administrator resets a password from **People → the person → Reset
  password**, or with `hubzoid admin reset-password <email>`. The current
  password stops working at once, the person's sessions end, and a one-time
  link lets them set a new one. `hubzoid admin reset-password <email>
  --password` sets one you type instead.
- `ENABLE_LOGIN_FORM=false` or `ENABLE_PASSWORD_AUTH=false` turns password
  sign-in off, so people use an external provider only.

## One-time sign-in links

An administrator gets a link to share when they add a user on **People**,
reset a password, or run `hubzoid admin create` or `hubzoid admin
reset-password`. The link opens `/auth/set-password?token=...`, where the
person sets a password and is signed in.

- A link lasts 72 hours (`HUBZOID_LINK_HOURS`) and works once.
- A newer link for the same person, or any password change, cancels it.
- Using it ends the person's other sessions.
- Only a SHA-256 digest of the token is stored.
- Settings builds the link on `HUBZOID_PUBLIC_URL` when it is set, else on
  the address you are using. `hubzoid admin` uses `HUBZOID_PUBLIC_URL`, else
  `http://127.0.0.1:<PORT>` (3080 by default).
- Hubzoid sends no email. Share the link directly with the person.
- The bridge removes link tokens and sign-in codes from its access log. The
  public port's access log still records the path of a link in this release,
  so treat those logs as sensitive.

## Google

1. In the [Google Cloud console](https://console.cloud.google.com/), open
   **APIs & Services → OAuth consent screen** for your project. Choose
   **Internal** if everyone signs in with your Google Workspace, **External**
   otherwise. The default scopes (`openid email profile`) are enough.
2. **Credentials → Create credentials → OAuth client ID**, application type
   **Web application**.
3. Add the authorized redirect URI `https://hub.example.com/oauth/google/callback`
   (your `HUBZOID_PUBLIC_URL`, exact path, no trailing slash).
4. Put the client ID and secret in the environment:

```dotenv
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true   # let Google sign in to the account with that email
OAUTH_ALLOWED_DOMAINS=example.com    # optional: only these email domains
# GOOGLE_OAUTH_SCOPE=openid email profile
```

The sign-in page then shows **Continue with Google**. With
`OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`, an account an administrator created
(with a password, or **Google sign-in only**) signs in with Google the first
time and stays linked. A Google Workspace domain listed in
`OAUTH_ALLOWED_DOMAINS` must also be the account's Workspace domain (the `hd`
claim), so a personal Google account registered with a work address is
refused. `gmail.com` addresses pass only when you list `gmail.com`.

## Microsoft (Entra ID)

1. Register an application in Microsoft Entra ID with the **Web** platform and
   the redirect URI `https://hub.example.com/oauth/microsoft/callback`.
2. Create a client secret.
3. Add the optional claim `xms_edov` to the ID token in the app
   registration's token configuration (see below).

```dotenv
MICROSOFT_CLIENT_ID=...
MICROSOFT_CLIENT_SECRET=...
MICROSOFT_CLIENT_TENANT_ID=<your tenant id>   # default: common
# MICROSOFT_OAUTH_SCOPE=openid email profile
```

Microsoft does not send `email_verified`. Hubzoid counts a Microsoft email as
verified only when the ID token carries `xms_edov` (the email's domain is
verified by its owner). Without it, a Microsoft sign-in cannot attach to an
existing account by email and cannot pass `OAUTH_ALLOWED_DOMAINS`. It can
still sign in to an account it is already linked to, for example one moved
from Open WebUI.

## Standard OpenID Connect

Any provider that publishes an OpenID Connect discovery document works:
Okta, Auth0, Keycloak, authentik, Zitadel and others. One such provider can be
configured.

```dotenv
OPENID_PROVIDER_URL=https://idp.example.com/.well-known/openid-configuration
OAUTH_CLIENT_ID=...
OAUTH_CLIENT_SECRET=...       # leave empty for a public client (PKCE only)
OAUTH_PROVIDER_NAME=Okta      # the button reads "Continue with Okta". Default: SSO
# OAUTH_SCOPES=openid email profile
```

Register the redirect URI `https://hub.example.com/oauth/oidc/callback`.
`OPENID_PROVIDER_URL` may also be the issuer URL: Hubzoid adds
`/.well-known/openid-configuration`. The provider must:

- serve discovery and its endpoints over https (plain http only on
  `localhost`),
- sign ID tokens with an asymmetric algorithm (RS, PS, ES or EdDSA),
- send an `email` claim in the ID token or from its userinfo endpoint, and
  `email_verified` true for linking by email and for `OAUTH_ALLOWED_DOMAINS`.

Group and role claims are not read. Access comes from grants to people in
Settings (see [administration](ADMINISTRATION.md)).

## How an external sign-in finds its account

Every external sign-in uses the authorization code flow with PKCE (S256),
`state` and `nonce`. The short handshake travels in an encrypted, HttpOnly
cookie (`hz_oauth`, path `/oauth`, 10 minutes). The ID token's signature is
checked against the provider's published keys, then its issuer, audience,
expiry and nonce. Then, in order:

1. **Already linked.** A sign-in is identified by the provider's issuer and
   subject. A linked sign-in goes to its account. Links moved from Open WebUI
   are found by provider and subject, and take the real issuer at their first
   sign-in.
2. **An invited account with the same email.** The sign-in is linked to it
   only when the provider says the email is verified,
   `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` is true, and the account is an invitation
   nobody has used yet (never signed in, no other provider linked). An account
   already in use is never taken over by email: its owner signs in the usual
   way and adds the provider under **Settings → Security → Link**. Otherwise
   the person sees `email_not_verified` or `not_linked`. Hubzoid never links on
   an unverified email.
3. **No account.** With `ENABLE_OAUTH_SIGNUP=true` a new account is created
   (see [self sign-up](#self-sign-up-and-approval)). Without it the person
   sees "There's no account for that email yet" (`no_account`).

`localhost` addresses never sign in with an external provider.
`OAUTH_ALLOWED_DOMAINS` (comma-separated, unset or `*` for any) applies to
every provider and is judged on a verified email only. For a sign-in that is
already linked but carries an unverified email, the account's own email is
judged.

The callback address is built from the address the person used when it is
`HUBZOID_PUBLIC_URL` or one of `HUBZOID_ALLOWED_ORIGINS`, else from
`HUBZOID_PUBLIC_URL` (with neither set, from the address the person used).
Register a redirect URI for every address people use.

## Self sign-up and approval

Both kinds of self sign-up are off by default.

- `ENABLE_SIGNUP=true` adds **Create an account** to the sign-in page. A new
  password account always waits for approval, because its email is not
  verified.
- `ENABLE_OAUTH_SIGNUP=true` creates an account at a person's first external
  sign-in. It waits for approval, unless the provider verified the email and
  its domain is listed explicitly in `OAUTH_ALLOWED_DOMAINS` (not `*`). Then it
  is active at once.

An account waiting for approval cannot sign in. An organization administrator
approves it under **People → the person → Approve**, or with `hubzoid admin
set-role <email> user` (or `admin`). A new account has no access to any agent
until someone grants it.

## Sessions

- The session cookie `hz_session` is HttpOnly, `SameSite=Lax`, path `/`, and
  `Secure` when the browser used https (a TLS proxy's `X-Forwarded-Proto`
  counts). It holds 32 random bytes. The database stores only their SHA-256
  digest.
- A session lasts at most `HUBZOID_SESSION_DAYS` (30) days and ends after
  `HUBZOID_SESSION_IDLE_DAYS` (7) days without use. Lowering either setting
  applies to existing sessions too.
- A session ends when the person signs out, changes their password (their
  other sessions), or uses a one-time link (their other sessions), and when an
  administrator resets their password, changes their role, blocks them or
  deletes them. A blocked person's sessions stay ended if the block is lifted.
- Every bridge of a gateway shares the session store, so one sign-in covers
  every agent the person may use.
- Changes from a browser must come from the deployment's own page (the
  `Origin` header must match the host or a configured origin).

- At most ten sessions per person: signing in on an eleventh device signs the
  least recently used one out. Each person sees their devices under
  **Settings → Security** and can sign one, or all the others, out.

## Two-step sign-in

People can turn on a second step under **Settings → Security**: after the
password (or Google, or OIDC), a 6-digit code from an authenticator app such
as Google Authenticator, 1Password or Microsoft Authenticator.

- **Who must use it.** `none`, `admins` (the default) or `all`, chosen by an
  administrator under **Workspace settings → Sign-in**, through
  `PUT /api/auth/mfa/policy`, or with `hubzoid auth mfa policy admins
  --server ...`. Before anyone chooses, `HUBZOID_MFA_REQUIRED` gives the
  default. Someone who must use it and hasn't set it up is taken through set-up
  right after their password, on one screen, and is never locked out.
- **Set-up.** Scan the QR code (or type the key), enter the first code, then
  save ten recovery codes. Each recovery code works once; **Get new recovery
  codes** replaces the set.
- **Codes.** Six digits, 30-second steps, one step either side. Each code works
  once, even if two requests race. Five wrong codes end the sign-in step.
- **Lost phone.** Sign in with a recovery code, then **Set up a new
  authenticator**. Or an administrator uses **People → the person → Reset
  two-step sign-in**. That also signs them out everywhere, and they set it up
  again at their next sign-in.
- A password reset keeps two-step sign-in on. The authenticator secret is
  encrypted with the deployment key; recovery codes are stored as digests only.
- People who sign in through a company directory (SAML) use that provider's own
  sign-in policy, including its MFA, and aren't asked for a second code.

## Confirm it's you

Some changes ask the person to confirm who they are when their last check was
more than ten minutes ago: creating an API key, changing two-step sign-in,
weakening the two-step sign-in rule, resetting someone else's two-step
sign-in, managing company sign-in, and **Sign out everywhere**. They confirm
with their password, and a code when they use two-step sign-in. An account
without a password confirms by signing in with its provider again. An API key
can never confirm.

## Sign out everywhere

**Settings → Security → Sign out everywhere and end all keys**, or **People →
the person → Sign out everywhere** for an administrator, ends every session
except the current one, every personal API key and every assistant (MCP)
connection at once. It does this by moving the account's credential counter.
The same happens when a person is blocked, deleted or turned off by the
company directory, and when their password is reset.

## Personal API keys

A key lets a script, a CI job or the Hubzoid command line act as one person.
Create one under **Settings → Security → API keys** or with `hubzoid auth keys
create "CI evals" --preset read --server ...`. The key (`hzp_...`) is shown
once; Hubzoid keeps only its digest. Send it as `Authorization: Bearer hzp_...`.

| Preset | Operations | What it reaches |
|---|---|---|
| Chat with my agents | `chat` | `/api/chat`, `/api/conversations`, `/v1/chat/completions`, `/v1/models`, the agent list |
| Read only | `read` | Any read (GET) the person can make |
| Everything I can do | `chat`, `read`, `manage` | Any change the person can make, except the list below |

- A key never does more than its owner can do right now: blocking, deleting or
  removing someone's access applies to their keys at once.
- A key limited to some agents can only chat, only with those agents, and only
  through the OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`).
- Behind a gateway (several agents on one address), each agent has its own API
  base: `https://<host>/b/<agent>/v1`. The bare `/v1` is the first agent, and a
  `model` naming another agent is refused with 404, never answered by the
  first. Without a personal key, `/v1` answers 401.
- A `chat` key writes only your own chats, files and organizing, never the
  workspace's chat settings. On the bridge, file and upload routes take only
  the bridge's own keys.
- Keys expire after 30, 90 or 365 days.
- A key can never make or revoke keys, change two-step sign-in or the password,
  confirm a sign-in, sign devices out, or approve requests.
- A request carrying both a session cookie and a key is refused
  (`ambiguous_credentials`). An unknown key never falls back to the cookie or
  the local owner.

## The command line against a hosted Hubzoid

```bash
hubzoid auth login --server https://team.example.com   # password, then a code
hubzoid auth whoami
hubzoid auth keys list
hubzoid auth sessions revoke-others
hubzoid api GET /api/agents
HUBZOID_API_KEY=hzp_... hubzoid api GET /api/me/access
```

`--server` or `HUBZOID_SERVER` names the deployment. `HUBZOID_API_KEY`, or the
session saved by `hubzoid auth login` (in `~/.config/hubzoid/credentials.json`,
readable by you only, holding the session token, never the password), signs
the requests. Exit codes: 0 done, 2 invalid input, 3 not allowed, 4 conflict,
5 unavailable, 6 accepted but not finished. A remote failure never falls back
to local files.

## From a laptop hub to team sign-in

A hub that ran without sign-in has a local owner (`admin@localhost`) that owns
its conversations. Keep them by turning that owner into a real account:

```bash
hubzoid auth bootstrap ./my-hub --email you@example.com --password-stdin
```

It keeps the same account id (so the conversations stay yours) and gives the
new email the local owner's access. Then set `HUBZOID_AUTH=true` and restart.

## Company sign-in (SAML and SCIM)

Install the optional extra and `xmlsec1`:

```bash
pip install "hubzoid[saml]"
brew install libxmlsec1        # macOS; Debian and Ubuntu: apt-get install xmlsec1
```

Then, under **Workspace settings → Sign-in → Company sign-in**:

1. **Add identity provider.** Choose a short id (it goes in the URLs) and the
   button label, and give the identity provider's metadata URL or paste its
   XML.
2. **Paste the values shown** into Okta or Microsoft Entra ID: the Entity ID
   (audience) and the sign-in URL (ACS, HTTP-POST). Send the person's email or
   userName as the NameID.
3. **Create a SCIM token** and give it, with the SCIM base URL, to the identity
   provider's provisioning settings. Provision users, not groups (group push
   isn't supported yet).
4. **Test sign-in**, then **Turn on**. People see "Continue with <label>" on the
   sign-in page.

How it behaves:

- **Who exists.** The directory decides, through SCIM. A provisioned account is
  managed: it signs in only through its provider and has no Hubzoid password.
  An email that already has a Hubzoid account isn't joined automatically.
- **Who signs in.** Only signed assertions answering a request this browser
  started are accepted, each once. The NameID must match exactly one
  provisioned person (by SCIM userName). Nobody is created at sign-in.
- **Offboarding.** Deactivating or deleting a person in the directory turns
  their account off and ends their sessions, API keys, MCP connections and
  scheduled work, in one step. Only the directory can turn it back on. The
  account and its conversations are kept.
- **Not yet:** SAML single logout, LDAP, group sync, and joining existing
  accounts to a directory.

## Rate limits

Failed sign-ins are counted per client address and per email, in the shared
store, so every bridge enforces the same limits.

- After `HUBZOID_AUTH_MAX_FAILURES` (10) failures within 15 minutes, the
  address or the email is locked for 15 minutes. The tenth failure answers
  `429 rate_limited` with `Retry-After`.
- Password sign-in, password changes and self sign-up are counted.
- Self sign-up also has its own per-address counter (`signup:<address>`).
  Every sign-up counts, including a successful one, and success does not
  clear it. After the same window limit, further sign-ups from that address
  are refused with `429 rate_limited`. A successful sign-in still clears only
  the sign-in counters.
- Loopback addresses are not counted per address, so a missing client address
  never locks everyone out. The per-email limit always applies.
- The client address comes from `X-Forwarded-For`. Behind a TLS proxy that
  sets it, each person is counted on their own address. Without such a proxy,
  a client can send its own `X-Forwarded-For`, so the per-address limit is
  easy to avoid. See [reverse proxy and TLS](DEPLOYING.md#6-reverse-proxy--tls-optional-recommended).
- Sign-ins, failed sign-ins to existing accounts, sign-outs, password changes,
  used links and sign-ups appear in Settings → **Activity** for
  organization administrators.

## More than one public address

```dotenv
HUBZOID_PUBLIC_URL=https://agents.example.com
HUBZOID_ALLOWED_ORIGINS=https://agents.internal.example.com,http://localhost:3080
```

Every listed address may send changes from a browser, gets OAuth callbacks on
its own address, counts as this machine in local mode, and may receive
personal connection callbacks. Links Hubzoid writes (one-time links, download
links) use `HUBZOID_PUBLIC_URL`. When no public URL is set, a local `hubzoid
run` on a loopback address sets it to its own address and allows the other
spelling (`localhost` or `127.0.0.1`) for you.

Behind a gateway, every agent accepts each address the deployment names: the
gateway's `--public-url` and `HUBZOID_ALLOWED_ORIGINS`, and each hub's own
`HUBZOID_PUBLIC_URL` and `HUBZOID_ALLOWED_ORIGINS`. The gateway
records them when it starts, so restart it after adding an address.

## The admin command

`hubzoid admin` works on the deployment's account store without signing in,
with the authority of whoever runs it on the server. Pass any hub of a gateway,
or run it in a hub folder. It refuses a deployment that still has Open WebUI
settings that stop the start, since its accounts are still in Open WebUI.
Passwords are typed at a hidden prompt, never passed as arguments, and
never printed.

| Command | What it does |
|---|---|
| `hubzoid admin create <email> [hub] [--name NAME] [--admin] [--owner] [--password] [--google-only]` | Creates an active account. `--admin` makes an Administrator, `--owner` also gives the owner's access on every hub. Prints a one-time link unless `--password` or `--google-only` |
| `hubzoid admin reset-password <email> [hub] [--password]` | Ends the current password and the person's sessions, then prints a link (or sets the password you type) |
| `hubzoid admin list [hub]` | Every account: role, status, how they sign in, last sign-in |
| `hubzoid admin set-role <email> admin\|user [hub]` | Sets the Administrator role in Hubzoid and in the web app together, and approves an account that was waiting. Refuses to demote the last administrator |

`--google-only` creates an account with no password. It can sign in only when
Google is configured with `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`. Changes are
recorded in **Activity** with the operator as `cli:<user>@<host>`.

## After turning sign-in on

The local owner keeps its account and its grants, but nobody can sign in as
it. Scheduled workflows that ran as `admin@localhost` keep running as it until
you set `HUBZOID_WORKFLOW_USER` or `run_as` to a real account (see
[workflow identity](workflow-identity.md)).

## Troubleshooting

| Symptom | Check |
|---|---|
| The provider says the redirect URI does not match | Register `<your address>/oauth/google/callback` (or `microsoft`, `oidc`) exactly, with no trailing slash, for every address in `HUBZOID_PUBLIC_URL` and `HUBZOID_ALLOWED_ORIGINS` people use |
| `/auth?error=sign_in_off` | Sign-in is off. Set `HUBZOID_AUTH=true` and restart |
| `/auth?error=not_configured` | The provider's client ID or secret is missing in the environment of the process that serves the web app |
| `/auth?error=not_linked` | The email has an account, but `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` is not true |
| `/auth?error=email_not_verified` | The provider did not vouch for the email. For Microsoft, add the `xms_edov` claim |
| `/auth?error=domain_not_allowed` | The email's domain is not in `OAUTH_ALLOWED_DOMAINS`, or a Google Workspace domain does not match |
| `/auth?error=state_mismatch` | The sign-in took longer than 10 minutes, or started on another address. Start again from the sign-in page |
| "This request came from another site" | The page's address is not `HUBZOID_PUBLIC_URL` or in `HUBZOID_ALLOWED_ORIGINS` |
| "Too many attempts" | Wait 15 minutes, or reset the password with `hubzoid admin reset-password` |
| Nobody can administer | `hubzoid admin set-role <email> admin <hub>` on the server |
| Signed in, but no agent | The account has no access yet. An administrator grants **Use this agent** in Settings |

## Coming from Open WebUI (1.x)

`hubzoid migrate openwebui` moves Open WebUI accounts, their passwords and
their Google, Microsoft and OpenID Connect links (see
[upgrading](UPGRADING.md#upgrading-to-20-open-webui-removed)). GitHub, LDAP,
trusted proxy headers and claim-based roles and groups were Open WebUI
features; the web app does not offer them. Accounts that used them need a
password link or one of the providers above.
