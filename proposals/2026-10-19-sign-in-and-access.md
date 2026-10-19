# Sign-in and access for Hubzoid 2.0

## Problem
Today sign-in covers the basics and access works, but a team can't run on it safely:

- **No two-step sign-in.** Administrators can't be asked for a code from an app.
- **No personal API keys.** Scripts, CI and the command line can't act as a
  person. Every JSON API accepts only a browser cookie, and `/v1/chat/completions`
  accepts only one shared server key.
- **Disabling someone doesn't end everything.** A blocked person's hosted MCP
  grants keep working (`hubzoid/mcp_oauth.py`).
- **Deleting a person deletes their conversations.**
- **No company sign-in.** There is no SAML or SCIM, so turning someone off in
  Okta or Microsoft Entra ID changes nothing here.
- **Access checks are scattered.** There is no single check, and tools don't
  recheck access when they run. A run started before a revocation keeps going.
- **No record of who a run is for.** Workflows record who they run as; chat,
  MCP, API and schedules don't.

## Why now
2.0 makes the native chat the only interface, and the next pieces build on
these. Approvals (human-in-the-loop) need a record of who asked. Hosting needs
API keys and a remote CLI. Teams need company sign-in.

## What should happen

### Sign-in
- **Two-step sign-in.**
  - Authenticator codes (TOTP, computed with the standard library) plus ten
    one-use recovery codes.
  - The rule is set in Settings, the API or the CLI: no one, administrators
    (the default), or everyone.
  - People who must use it set it up during sign-in, on one screen: a QR code
    or key, then the first code, then recovery codes to copy or download.
  - The code field auto-submits, accepts pasted codes and has a Verify button.
    Five wrong codes end the step.
- **Recent sign-in for sensitive changes.** Creating a key, changing two-step
  sign-in, changing the rule or resetting someone's MFA asks "Confirm it's you"
  (password, then a code if enrolled) when the last check is older than ten
  minutes. A provider-only account confirms with its provider.
- **Devices.** A list of signed-in browsers ("Chrome on macOS", a short
  address, last active), with sign-out per device and "Sign out of all other
  devices". At most ten sessions per person.
- **One credential counter per account.** A block, a deletion, a password reset,
  a directory deactivation or "Sign out everywhere" ends sessions, API keys,
  MCP grants and pending sign-in steps in the same transaction, on every
  instance.
- **Personal API keys** (`hzp_…`, shown once, stored as a digest).
  - A key does chat, read or manage. It can be limited to some agents, but then
    it can only chat, through `/v1`. Conversations span agents.
  - Chat writes only the person's own chats, files and organizing, never the
    workspace's chat settings.
  - It expires in 30, 90 or 365 days, and never does more than its owner can
    right now.
  - A key never makes keys, changes two-step sign-in, confirms a sign-in or
    decides an approval.
  - It works on every route that checks sign-in, including `/v1/chat/completions`,
    where its chats live in the owner's own space. The bridge's file and upload
    routes take only the bridge's own keys.
- **Remote CLI.**
  - `hubzoid auth login --server URL` signs in with a password and code; only
    the session token is saved, owner-only.
  - `hubzoid auth whoami|keys|sessions|mfa` manage your account.
  - `hubzoid api METHOD PATH` calls any route as you.
  - `HUBZOID_API_KEY` works too.
  - A remote failure never falls back to local files.
- **Laptop to team.** `hubzoid auth bootstrap --email … --password-stdin` turns
  the local owner into a password account with the same id and grants.
- **Deleting a person keeps a tombstone.** Their conversations stay attached
  to their id, their share links stop, and the email can be used again.
- **Provider email matching** only completes invitations nobody has used yet.
  An account in use is never taken over by email. Its owner adds the provider
  from Settings > Security > Link.
- **The deployment id is stored once**, so a restore or a new mount path keeps
  it.

### One access check
- `access.evaluator.authorize` decides for chat, Settings, the API (session or
  key), the CLI, MCP, workflows and schedules. There is no administrator
  bypass, and a store error refuses.
- A decision is cached until the access store's revision or the account's
  counter moves.
- **Every tool call rechecks** that the run's account is still active,
  unrevoked, unblocked and allowed to use the agent, and that the session or
  key that started it still works. This covers the Claude, Codex and Agents SDK
  runtimes, Claude's own tools (PreToolUse), and outside MCP servers (checked on
  each server call).
  - An allowed call runs at once: tools never ask a person.
  - Only approvals (human-in-the-loop) and approve-mode writes ask someone.
- **A test asks every API route signed-out** on a real bridge and fails if any
  answers without sign-in. Routes public on purpose are listed, with the reason.
- **"What you can use"** on the Profile tab: agents and features, read only.

### Who a run is for
- **A record per run.** Before any model or tool runs, every chat reply, API
  call, MCP call, channel message (WhatsApp or Telegram matched by phone,
  Slack), workflow run and schedule run gets a record that never changes. A run
  whose record can't be written doesn't start. It holds:
  - who it runs as, who asked, and where it started;
  - whether someone is watching (chat only);
  - the credential that started it and the account's counter at that time.
- **Recovery reuses the first record.** A workflow recovering after a restart
  gets the record it was admitted with. A run from before records existed shows
  as legacy.
- **"Who ran this"** on a chat reply shows it in plain words.

### Company directories
- **SAML 2.0 through pysaml2.**
  - Accepted: signed assertions, answering a request this browser started,
    each used once.
  - An unsolicited answer starts a fresh sign-in instead.
  - The NameID must match one provisioned person: by the directory's id
    (`externalId`) first, else by `userName` (an email NameID).
  - Provider-initiated "confirm it's you" and linking ask the provider for a
    fresh sign-in.
- **SCIM 2.0 for Users** (scim2-models): create, list with filters, get, PUT,
  PATCH, DELETE and ETags. It passes scim2-tester's User checks. Groups answer
  "not supported".
- **Directory-managed accounts** sign in only through their provider and have no
  password.
- **Deactivating a person in the directory** ends every credential at once. Only
  the directory can turn the account back on, and the account and its
  conversations are kept.
- **Administrators set it up** in Workspace settings > Sign-in. They paste the
  identity provider's metadata, copy the values to paste back, test sign-in,
  turn it on and create SCIM tokens.

## Scope and non-goals
Not in this change:
- groups and per-group features;
- an access file with preview and undo;
- a sign-in settings file;
- trusted-proxy sign-in;
- password reset and invites by email;
- GitHub and Linear identity links;
- LDAP;
- joining existing accounts to a directory;
- SAML single logout.

Grants stay keyed by email, with each email bound to its account id. New
records are keyed by account id.

## Compatibility
- **Migrations.** `op_0032` and `op_0033` only add columns and tables.
- **Two-step sign-in for administrators** is required by default after the
  upgrade. Set `HUBZOID_MFA_REQUIRED=none`, or pick another rule in Settings.
- **The optional extra `hubzoid[saml]`** adds pysaml2 and scim2-models. SAML
  also needs the `xmlsec1` program. The core install gains `qrcode`.
- **The Docker try-out without sign-in** keeps working through its explicit
  flag, now with a warning bar on every page.

## Definition of done
Observed on a test hub in the browser:
- an administrator sets up two-step sign-in during sign-in and signs in with a
  code and with a recovery code;
- a device is signed out from another browser;
- an API key chats with an agent and stops working once revoked;
- revoking credentials stops a running conversation's next tool call;
- a SCIM-provisioned person signs in with SAML and is shut out everywhere when
  the directory turns them off.
