# Authentication

By default a hub runs with no login - one user, localhost, no friction. For
multi-user, production, or any deployment past your laptop, turn auth on.

Auth is handled by Open WebUI via env vars in the hub's `.env`. Hubzoid does
not introduce a separate auth layer. Pick a mode, drop the lines in, restart.

| Mode | Use case |
|---|---|
| A. No auth (default) | localhost dev, single user |
| B. Email + password | small teams, no identity provider |
| C. Google / Microsoft / GitHub SSO | prod with consumer or workspace identity |
| D. Generic OIDC (Okta, Auth0, Keycloak, authentik, ...) | prod with an enterprise IdP |
| E. LDAP / Active Directory | enterprise on-prem identity |
| F. Reverse-proxy trusted header (oauth2-proxy, Cloudflare Access) | identity terminated upstream |

Each agent runs its own Open WebUI process with its own SQLite user database
under `<hub>/.openwebui-data/webui.db`. Adding alice@example.com to one agent
does not give her access to the others. This is intentional: agents are
independent products.

## Mode A: no auth (default)

Nothing to set. `WEBUI_AUTH` defaults to `False`. Anyone who can reach the
port is in. Fine for localhost dev. Not fine for anything else.

## Mode B: email + password

Six lines in `.env`. Admin invites everyone else.

```bash
WEBUI_AUTH=true
ENABLE_SIGNUP=false
DEFAULT_USER_ROLE=user
WEBUI_SECRET_KEY=<openssl rand -hex 32>
WEBUI_URL=https://your.host           # required behind a reverse proxy
WEBUI_ADMIN_EMAIL=you@example.com     # one-shot: seeds first admin
WEBUI_ADMIN_PASSWORD=<temp pass>      # one-shot: delete both ADMIN_ lines after first boot
```

Boot once. OWUI sees `WEBUI_ADMIN_*` on a fresh DB and seeds you as admin
without needing a public signup window. Restart hubzoid after deleting the
two `WEBUI_ADMIN_*` lines. Then sign in at `/` with the email and password
you set. From the admin panel, add the rest of your team.

Hubzoid refuses to boot if `WEBUI_AUTH=true` and `WEBUI_SECRET_KEY` is not
set, so that OWUI's public fallback secret (`t0p-s3cr3t`) never ends up
signing real session JWTs.

## Mode C: Google SSO

The most common ask. Three-minute setup if you already have a Google Cloud
account.

### In Google Cloud Console

1. console.cloud.google.com -> create or pick a project (e.g.,
   "example-agents-auth").
2. APIs & Services -> OAuth consent screen. User type: **Internal** if all
   sign-ins will be from your Google Workspace; **External** otherwise. Fill
   app name (shown on Google's consent screen - use your hub name),
   support email, developer email. Default scopes (`openid email profile`)
   are enough.
3. Credentials -> Create credentials -> OAuth client ID. Application type:
   **Web application**.
4. Authorized JavaScript origins: `https://your.host`
5. Authorized redirect URIs:
   `https://your.host/oauth/google/callback`
   - Exact path. No trailing slash. This is the one detail Google's docs
     underspecify and OWUI is strict about.
6. Save. Copy the **Client ID** and **Client secret**.

### In the hub's `.env`

```bash
# Auth (Mode B baseline)
WEBUI_AUTH=true
ENABLE_SIGNUP=false
WEBUI_SECRET_KEY=<openssl rand -hex 32>
WEBUI_URL=https://your.host

# Google SSO
ENABLE_OAUTH_SIGNUP=true
DEFAULT_USER_ROLE=pending             # new Google users wait for admin approval
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...

# Bootstrap (one-shot - delete after first boot)
WEBUI_ADMIN_EMAIL=you@example.com
WEBUI_ADMIN_PASSWORD=<temp pass>
```

### First boot

1. Start the hub. Admin is seeded from `WEBUI_ADMIN_*`.
2. Stop. Delete the `WEBUI_ADMIN_*` lines. Restart.
3. Open `https://your.host/`. Click **Sign in with Google**. Authenticate
   with the email matching `WEBUI_ADMIN_EMAIL`. You land as admin.
4. Admin Panel -> Users -> Add user. Pre-add each team member's Google email
   with role `user`.
5. Team members click **Sign in with Google**; their account matches the
   pre-added record; they're in.

### Adding and removing users

- New hire: admin opens the Users panel, clicks Add user, types their
  Google email.
- Departure: admin opens the same panel, clicks Suspend (keeps audit
  history) or Delete.

That's the whole loop. No CLI, no config file.

### Limiting sign-ins to a domain

```bash
OAUTH_ALLOWED_DOMAINS=example.com,partner.example.com
```

Any successful Google login outside these domains is rejected.

## Mode C variants: Microsoft and GitHub

Same shape as Google. Different env-var names and redirect paths.

### Microsoft (Entra ID / Azure AD)

```bash
MICROSOFT_CLIENT_ID=...
MICROSOFT_CLIENT_SECRET=...
MICROSOFT_CLIENT_TENANT_ID=common     # or your tenant GUID
MICROSOFT_OAUTH_SCOPE=openid email profile offline_access
```

Authorized redirect URI in the Azure app registration:
`https://your.host/oauth/microsoft/callback`

### GitHub

```bash
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_CLIENT_SCOPE=user:email
```

Authorized redirect URI in the GitHub OAuth app:
`https://your.host/oauth/github/callback`

## Mode D: generic OIDC (Okta, Auth0, Keycloak, authentik, ...)

Anything that publishes a `.well-known/openid-configuration` document
works.

```bash
OAUTH_CLIENT_ID=...
OAUTH_CLIENT_SECRET=...
OPENID_PROVIDER_URL=https://your-idp.example.com/.well-known/openid-configuration
OAUTH_PROVIDER_NAME=Okta              # button label on the login screen
OAUTH_SCOPES=openid email profile
```

Redirect URI to register with the IdP: `https://your.host/oauth/oidc/callback`

Role / group sync from OIDC claims (when your IdP carries them):

```bash
ENABLE_OAUTH_ROLE_MANAGEMENT=true
OAUTH_ROLES_CLAIM=roles
OAUTH_ALLOWED_ROLES=user,admin
OAUTH_ADMIN_ROLES=admin

ENABLE_OAUTH_GROUP_MANAGEMENT=true
OAUTH_GROUPS_CLAIM=groups
```

With group management on, OWUI re-syncs group membership on every login.

## Mode E: LDAP / Active Directory

```bash
ENABLE_LDAP=true
LDAP_SERVER_HOST=ldap.example.com
LDAP_SERVER_PORT=636
LDAP_USE_TLS=true
LDAP_SEARCH_BASE=dc=example,dc=com
LDAP_APP_DN=cn=svc-hubzoid,ou=users,dc=example,dc=com
LDAP_APP_PASSWORD=...
LDAP_ATTRIBUTE_FOR_USERNAME=sAMAccountName
LDAP_SEARCH_FILTER=(objectClass=person)
```

LDAP is a less-trodden path; expect to consult OWUI's LDAP docs as well.
The rest of the hubzoid behavior (per-agent user DB, admin invites) is the
same.

## Mode F: reverse-proxy trusted header

When auth is terminated by an upstream (oauth2-proxy, Cloudflare Access,
Authelia, an Nginx OIDC module), OWUI can trust headers the proxy sets.

```bash
WEBUI_AUTH=true
WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Forwarded-Email
WEBUI_AUTH_TRUSTED_NAME_HEADER=X-Forwarded-User
WEBUI_AUTH_TRUSTED_GROUPS_HEADER=X-Forwarded-Groups
WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-Forwarded-Role
```

> [!WARNING]
> OWUI does no IP or identity check on these headers. The reverse proxy
> **must** strip them from inbound client requests before forwarding,
> otherwise any browser can impersonate any user by setting the header
> itself. Bind OWUI to `127.0.0.1` and accept traffic only from the proxy.

The same proxy is the natural place to forward identity and groups on to the
hub bridge as `X-Hubzoid-User` / `X-Hubzoid-Groups`, which is what per-role tool
access reads. Login (this document) decides who gets in; access control decides
what they can call once inside. See [access-management.md](access-management.md).

## Per-agent vs shared SSO

Each agent has its own user DB. Two real options when you roll out multiple
agents:

| Option | Setup | Trade |
|---|---|---|
| Per-agent user lists | Each agent's `.env` has its own auth block. Admin invites users separately per agent. | Strong isolation. Different teams use different agents without seeing each other. |
| Shared Google / OIDC SSO | Same `GOOGLE_CLIENT_ID` (and same redirect URI registered once per agent) across all agents' `.env` files. User DBs stay separate but login is unified. | One sign-in covers all agents. Adding alice@example.com to one agent still doesn't auto-give her access to the others - that stays per-agent. |

Default for most rollouts: **shared SSO + per-agent user lists**. Users see
one Google button across all hub URLs; admins control which user gets which
agent.

## Common gotchas

- **Redirect URI mismatch.** The path is provider-specific:
  `/oauth/google/callback`, `/oauth/microsoft/callback`,
  `/oauth/github/callback`, `/oauth/oidc/callback`. No trailing slash.
- **`WEBUI_URL` unset behind a proxy.** OAuth callback URLs are built from
  `WEBUI_URL`. If it's still `http://localhost:3000`, the IdP will try to
  redirect the user back to localhost. Hubzoid refuses to boot in this
  case if you have OAuth env vars set.
- **`WEBUI_SECRET_KEY` unset.** OWUI uses a public fallback. Hubzoid refuses
  to boot when `WEBUI_AUTH=true` and the key is missing.
- **Users land in "pending" and don't see the chat.** Expected when
  `DEFAULT_USER_ROLE=pending`. Admin Panel -> Users -> set role to `user`.
- **Signed up first by accident; now you're not admin.** Stop the hub,
  delete `<hub>/.openwebui-data/webui.db`, set `WEBUI_ADMIN_*` env vars,
  restart. Fresh DB, you become admin.
- **OAuth settings stuck after edit.** Hubzoid forces
  `ENABLE_OAUTH_PERSISTENT_CONFIG=False` so env-var changes always win on
  restart. If you previously ran a build without this, clear
  `.openwebui-data/` or change the value via the admin panel once to
  unstick.

## Network exposure

- Bridge port (`BRIDGE_PORT`, default 8000) binds `127.0.0.1` only. Not
  reachable from outside the box. Protected additionally by
  `BRIDGE_API_KEYS`.
- Open WebUI binds `127.0.0.1:<PORT>`. A reverse proxy (Caddy, nginx)
  terminates TLS and forwards. Public exposure is via the proxy.
- See `docs/DEPLOYING.md` (coming with the native-venv prod doc) for
  Caddyfile + systemd templates.
