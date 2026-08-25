# Access control: per-role tool gating

Open WebUI decides who logs in (see [auth.md](auth.md)). This decides what a
logged-in person may do once inside: which tools they can call. The model is
never the gate. Every decision is deterministic code that runs before the tool,
and every decision is logged.

A hub with no `restricted/` folder is unaffected by any of this. Access control
is entirely opt-in: you turn it on by adding the folder.

## The model in one line

A critical tool lives in a file. The file name is the permission. An Open WebUI
group of the same name is the key.

```
restricted/ornate.py   ->  permission "ornate"   ->  OWUI group "ornate"
restricted/finance.py  ->  permission "finance"  ->  OWUI group "finance"
```

Being in the group unlocks every `@function_tool` in that file. The match is
case insensitive and whitespace trimmed, so `Ornate`, `ornate`, and `" ornate "`
are the same door.

## Setup

### 1. Put the tool in `restricted/`

Same as `tools_local/`, but the folder is `restricted/` and the file name is the
permission. Name the file by what it should mean to the client (`sales.py`,
`finance.py`), not necessarily by the system behind it.

```python
# <hub>/restricted/ornate.py
from agents import function_tool

@function_tool
def ornate_sales(store: str = "ALL") -> str:
    """Sales from the Ornate ERP."""
    ...
```

### 2. Create the matching Open WebUI group

In Open WebUI, create a group named `ornate` and add the people who should reach
that tool. Group membership is the grant. A person in two groups has both. There
is no separate grant screen: Open WebUI's own group management is the admin UI.

### 3. That is it, with Open WebUI

Nothing else to wire. Open WebUI forwards the logged-in user's email to the
bridge, and hubzoid looks up that user's groups in Open WebUI's own database (the
Groups screen from step 2). So adding a person to the `ornate` group grants them
the `ornate` permission on their **next message**. No proxy, no logout, no
restart. Removing them revokes it just as fast. This is the client-editable
model: the admin manages access entirely in the Open WebUI Groups UI, with no
developer.

(Under the hood: hubzoid sets `ENABLE_FORWARD_USER_INFO_HEADERS` when it launches
Open WebUI, so each request carries `X-OpenWebUI-User-Email`; the bridge resolves
that email to the user's groups via OWUI's `group_member` table, read-only and
fail-closed.)

**Other fronts, or a custom identity source (advanced).** The bridge also accepts
explicit headers from a trusted reverse proxy. Groups are **unioned**, not
overridden: a caller carries their OWUI groups, their roster groups (see below),
and any header groups together.

| Header | Meaning |
|---|---|
| `X-Hubzoid-User` | the user id (display + audit) |
| `X-Hubzoid-Groups` | comma-separated group names; unioned onto the OWUI + roster groups |
| `X-Hubzoid-Surface` | the front the request came from (default `owui`) |

**Roster parity across surfaces.** A hub's `identity/access.csv` (or `access.py`)
is now consulted on the Open WebUI and MCP paths too, keyed by the logged-in
email, not only on WhatsApp/Telegram. So a coordinator granted a group in the
roster gets the matching permission on every surface, with no duplicate OWUI
group to maintain. This is **additive**: an email absent from the roster keeps
exactly its OWUI groups (nobody is locked out), and the roster can grant
restricted-tool permissions but **cannot** open the MCP front door
(`MCP_ACCESS_GROUP`), which stays OWUI-admin-only. Editing the CSV takes effect
on the next request — no restart. A live `access.py` backing must define
`groups_for_email(email)` to participate on these surfaces; a legacy file with
only `resolve(surface, handle)` is never handed an email.

Use these when you terminate auth at a proxy (see [auth.md](auth.md) Mode F) or
front the hub with something other than Open WebUI. They are trusted because
reaching the bridge already requires its API key, and end users talk to the
front, never to the bridge directly. A request that resolves to no groups can
reach no restricted tool: fail closed is the default.

## What is enforced, and where

Two layers, both built from the same tool registry, so both backends are covered:

1. **Hidden.** The agent is offered only the tools the current user may use.
   An ungranted restricted tool is not shown at all (OpenAI Agents SDK, via
   per-run `is_enabled`). The agent never sees a door it cannot open.
2. **Denied.** Every restricted tool re-checks at call time and fails closed,
   writing the decision to the audit log. This holds even if the tool is reached
   another way (a prompt injection naming it, the Claude backend, a test).

The deny layer is the wall. The hidden layer is the clean experience. The Claude
backend gets the deny layer (it does not consult `is_enabled`); the OpenAI
backend, the default, gets both.

## Surfaces: Slack and scheduled runs

A restricted door needs a verified person behind it. Open WebUI carries that.
Slack, Telegram, and scheduled background runs do not, so they get the
non-restricted tools only, and a restricted door is never reachable from them.
The Slack adapter declares `X-Hubzoid-Surface: slack` so this is enforced, not
assumed. Scheduled tasks run with no user, so they are anonymous and refused
every restricted tool by the same fail-closed default.

To change which surfaces may reach restricted tools, set
`HUBZOID_RESTRICTED_SURFACES` (comma-separated). Default: `owui,web,api`.

## Secrets

Secrets for restricted tools live in `restricted/.env`, which the runtime loads
into the process environment at startup. The file-reading tools (`read_file`,
`list_files`, `grep_data`) refuse any path under `restricted/`, so the model
cannot read a credential by reading the file, even though the restricted tool's
own code reads it to do its work. The model only ever sees the tool's result,
never the secret.

```
<hub>/restricted/.env        # ORNATE_PASSWORD=...  (model cannot read this)
<hub>/restricted/ornate.py   # the tool that uses it (gated by the ornate group)
```

This protects ordinary secrets well. For a crown-jewel secret like an SSH key
into a client's production system, hold it in a separate process under a
different operating system user, so the kernel keeps the agent out rather than a
denylist. That is a per-secret call, not the default.

## The audit log

Every allow and every deny is written where the decision is made, the runtime,
because Open WebUI never sees a tool call. One JSON line per decision, in
month-partitioned files so nothing grows without bound:

```
<hub>/logs/access-2026-06.jsonl
{"ts": "...", "user": "anjali", "surface": "owui", "tool": "ornate_sales", "decision": "deny", "reason": "no-group"}
```

Read it with:

```bash
hubzoid audit <hub>              # recent decisions
hubzoid audit <hub> --denied     # only refusals
hubzoid audit <hub> --user priya # one person
```

## Not yet here

Per-person row and field scoping (a branch manager seeing only their own store's
rows) is a separate axis, enforced at the data layer from the same verified
identity, not by files or groups. It is out of scope for this layer. The tool
gate answers "can this person touch Ornate at all," not "which rows."
