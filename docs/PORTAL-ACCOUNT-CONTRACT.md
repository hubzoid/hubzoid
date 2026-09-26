# Portal account-state contract

How `/portal/api/access`, `/portal/api/people` and `POST /portal/api/people/block`
describe whether a person can use a hub, and *why* they can't when they can't.
The second half documents the account and change-request endpoints, and how a
caller authenticates.

## Callers and authority

Every endpoint accepts either:

- the Open WebUI session cookie, verified server-side with Open WebUI. Writes
  must come from the same origin (`Origin` or `Referer` host equals `Host`).
- `Authorization: Bearer sk-...`, an Open WebUI API key, verified against Open
  WebUI's key table. The caller acts as the key's owner. No `Origin` is needed.
  Any other `Authorization` value gets 401.

Authority is decided by `hubzoid/access/service.py` from the access store on
every write. A refusal from it has this body:

```json
{"detail": "Outside your access: ...", "code": "outside_ceiling"}
```

`detail` is safe to show. `code` is stable. Codes include `forbidden`,
`outside_ceiling`, `self_change`, `legacy`, `unknown_permission`, `blocked`,
`unavailable`, `conflict`, `account_exists`, `account_replaced`,
`invalid_password`, `invalid_request`, `accounts_unavailable`, `uncertain`,
`partial`, `not_found`, `expired`, `not_pending`, `plan_changed`,
`session_required` and `too_many`.

## Two block markers, one enforcement answer

The access store keeps two independent per-subject markers. Enforcement
(`GrantStore.is_suspended`, and therefore `can()` / `permissions_for()`) treats a
subject as blocked when **either** is set.

| Marker | Set by | Cleared by |
|---|---|---|
| `suspended` | An org admin blocking the person (`POST /people/block`), or the store's `account_replaced` safeguard when a known email re-appears under a new OWUI id. Blocking also **deletes every grant** for the subject. | `POST /people/block` with `suspended:false` ("Reactivate"). Grants are **not** restored. |
| `account_unavailable` | Open WebUI: the account is `role=pending` (signed up, not yet approved), or a complete directory read no longer lists an account that was previously bound. | Only by OWUI reporting the account as approved / present again (login, `POST /people/refresh`, or the 30 s visibility loop). An admin cannot clear it from the portal. |

The API exposes both, plus their OR:

| Field | Type | Meaning |
|---|---|---|
| `suspended` | bool | Admin block marker. Reactivate can clear this. |
| `account_unavailable` | bool | Chat-app-side block. Approve/restore the account in Open WebUI, then refresh. |
| `blocked` | bool | `suspended || account_unavailable`. Always equals what enforcement uses. **Gate "can I add access" on this, not on `status`.** |
| `status` | string | One display label, see precedence below. |

`blocked` is the only field that should drive "grants to this person will fail
(409)". `status` is for the badge.

## `status` precedence

Evaluated top to bottom; the first match wins.

| # | Condition | `status` |
|---|---|---|
| 1 | `suspended` | `blocked` |
| 2 | subject is `*` | `everyone` |
| 3 | subject starts with `workflow:` | `service` |
| 4 | no `owui_id` (pre-granted email, never signed in) | `awaiting-signup` |
| 5 | identity `pending` (OWUI signup awaiting approval) | `pending-approval` |
| 6 | `account_unavailable` (bound account missing from the directory) | `blocked` |
| 7 | otherwise | `active` |

Reading the combinations the UI will actually meet:

| `status` | `suspended` | `account_unavailable` | `blocked` | Situation |
|---|---|---|---|---|
| `active` | false | false | false | Normal user. |
| `awaiting-signup` | false | false | false | Granted by email; hasn't signed in yet. Not blocked. |
| `pending-approval` | false | **true** | **true** | Signed up, awaiting OWUI approval. Blocked until approved; Reactivate does nothing. |
| `blocked` | **true** | false | true | Admin block. Reactivate clears it. |
| `blocked` | false | **true** | true | Account deleted / missing in OWUI. Reactivate does nothing; restore in OWUI. |
| `blocked` | **true** | **true** | true | Admin block on a pending or missing account. Reactivate clears the admin part only; `status` then becomes `pending-approval` or stays `blocked` with `account_unavailable=true`. |

Before this change `status` tested `is_suspended` first, so a pending user always
rendered as `blocked` and `pending-approval` was unreachable.

## Endpoints

### `GET /portal/api/me`

`subject`, `org_admin` and `manageable` as before, plus:

| Field | Meaning |
|---|---|
| `grantable` | `{hub: [permission]}` this caller may grant or remove in each hub they manage. A delegate's list is their current access there minus `manage_access`. A legacy hub has `[]`. |
| `account_admin` | Whether the caller may approve, reset, change the role of or delete accounts (organization administrators). |
| `can_create_accounts` | Organization administrators, and delegates managing at least one managed hub. |
| `accounts_configured` | Whether the internal Open WebUI URL and service account are set. No network call. |
| `via` | `session` or `api-key`. |

`GET /me?brief=1` omits these (the chat sidebar link polls it).

### `GET /portal/api/access?hub=…`

Each element of `rows[]` now carries `suspended`, `account_unavailable`,
`blocked` and the corrected `status` alongside the existing fields (`subject`,
`display`, `kind`, `perms`, `inherited`, `effective`, `center`). The response
also carries `grantable` (the caller's ceiling in this hub, display only) and
`viewer` (the caller's subject).

Note: an admin block deletes all grants, so a `suspended=true` row normally
disappears from `/access` altogether. Rows with `blocked=true` on this endpoint
are almost always `account_unavailable` (pending or missing accounts keep their
grants).

### `GET /portal/api/people`

Each element of `people[]` carries the same four fields. `blocked` keeps its old
meaning (`is_suspended`). `pending` is the raw identity column and is **not** the
same as `status === "pending-approval"`: pre-granted emails also have
`pending=1` with no `owui_id` (that is `awaiting-signup`). Use `status`.

### `POST /portal/api/access/grant`

Unchanged behaviour, clearer 409s:

| Cause | 409 `detail` |
|---|---|
| `suspended` | `Reactivate this user before granting access` |
| `account_unavailable` only | `This account is unavailable in the chat app (awaiting approval or removed). Approve or restore it in Open WebUI, then refresh accounts.` |

Revokes to blocked subjects still succeed.

### `POST /portal/api/people/block` `{subject, suspended?: true}`

Org admin only. Returns the post-action account state instead of a bare
`{ok:true}`:

```json
{
  "ok": true,
  "subject": "bob@x.org",
  "changed": true,
  "message": null,
  "suspended": false,
  "account_unavailable": false,
  "blocked": false,
  "status": "active"
}
```

| Field | Meaning |
|---|---|
| `ok` | Request handled (kept for compatibility; always `true` on 2xx). |
| `changed` | Whether the store was written. Block: always `true`. Reactivate: `true` only if the admin marker was set. |
| `message` | `null`, or a sentence to show the admin when the person is **still blocked** after a reactivate. Prefixed `Admin block cleared.` when the admin marker was removed but OWUI still disallows the account, or `Not blocked by an admin.` when there was nothing to clear. |
| `suspended`, `account_unavailable`, `blocked`, `status` | State after the action, same semantics as above. |

Rules:

- A reactivate with `changed=false` writes nothing: no audit row, no marker
  change. This removes the misleading "reactivated" audit entry for a no-op.
- The `account_unavailable` marker is never touched by this endpoint. Enforcement
  is exactly as strong as before.
- Errors are unchanged: 403 for non-org admins or cross-origin, 409 for
  `LastAdminError` (blocking the last org admin), `*`, or an empty subject.

### `POST /portal/api/accounts`

```json
{"email": "ann@example.org", "name": "Ann", "password": "...",
 "grants": [{"hub": "finance", "permission": "ledger"}]}
```

Creates an Open WebUI account with role `user` and applies the grants in one
store transaction. Returns `{ok, subject, owui_id, name, role, grants, revision}`
and never the password. A delegate must include at least one grant, and every
grant must be within their ceiling. Validation errors name the fields and never
echo a value.

| Status and code | Meaning |
|---|---|
| 409 `account_exists` | The email already has an account. Grant access instead. Nothing was created. |
| 409 `account_replaced` | The email belonged to a deleted account. Only an organization administrator can re-create it. |
| 409 `blocked` | The email is blocked. Reactivate it first. |
| 403 `outside_ceiling`, `forbidden` | A grant the caller may not give. Nothing was created. |
| 422 `rejected`, `invalid_password` | Open WebUI or the minimum rule (8 characters, at most 72 bytes) refused the password. |
| 503 `accounts_unavailable` | No internal URL or service account. |
| 503 `uncertain` | Open WebUI did not answer. The account may exist. Refresh accounts first. |
| 502 `partial` | The account was created, granting failed and the automatic removal failed. The message names the account. |

If granting fails and the new account was removed, the original status and
code are returned with "The new chat account was removed, so nothing changed."

### Account actions (organization administrators)

| Method and path | Body | Notes |
|---|---|---|
| `GET /accounts/{subject}` | | `{subject, name, role}` read live from Open WebUI |
| `POST /accounts/{subject}/password` | `{password}` | Open WebUI revokes the account's sessions |
| `POST /accounts/{subject}/approve` | | `pending` to `user`, then the identity is marked available |
| `POST /accounts/{subject}/role` | `{role: "user"\|"admin"}` | The chat-app role only. Grants nothing in Hubzoid |
| `DELETE /accounts/{subject}` | `{confirm_email}` | Removes every grant, then the account. 409 `last_admin` if it would remove the final org admin. 502 `partial` if grants were removed but the account was not |

Each refuses your own account (`self_change`) and the service account
(`service_account`), and checks that the account linked to the subject still has
that email (`account_changed`).

### Change requests

Proposed by the agent tools (`HUBZOID_MANAGEMENT_TOOLS`). Only the proposer can
read, confirm or reject one. Anyone else gets 404.

| Method and path | Body | Notes |
|---|---|---|
| `GET /change-requests/{id}` | | The plan, `plan_hash`, status, expiry, current access and `problem` (why it could not apply now, or null) |
| `POST /change-requests/{id}/confirm` | `{plan_hash, password?}` | Session cookie only (403 `session_required` for a key). `password` is required for a new account |
| `POST /change-requests/{id}/reject` | | Session or key |

Statuses: `pending`, `applying`, `confirmed`, `rejected`, `expired`, `failed`.
A confirmation claims the request atomically, so it applies at most once, and
re-checks the proposer's current access. A request whose password Open WebUI
refused stays `pending`. Any other failure marks it `failed`.

## UI guidance

- After Block / Reactivate, render from the response body; a reload is not
  required to learn the outcome.
- If `blocked && !suspended` after Reactivate, show `message` and link to Open
  WebUI's admin users page, then offer "Refresh accounts" (`POST /people/refresh`).
- Disable *adds* in the Access drawer when `row.blocked`, not when
  `row.status === "blocked"` (pending users are blocked with a different label).
- Reactivate should only be offered when `suspended === true`.

## Limitations (honest list)

- The markers are read from the store's `hz_meta` table through its private
  `_meta_get` helper (`_account_flags` in portal.py and access/service.py). No
  public store method exposes them separately yet.
- "Missing from directory" and "pending approval" both set
  `account_unavailable`; they are told apart only by the identity's `pending`
  column, which `reconcile_accounts` does not reset for a vanished account. A
  user who was pending and then deleted keeps `status=pending-approval`.
- The Access editor saves one person's change set atomically
  (`POST /access/apply`). There is no multi-person batch.
- A change request interrupted while `applying` (a process crash) stays in that
  state. Propose it again.
- Reactivate does not restore the grants that blocking deleted.
- Each `/access` row and `/people` entry now costs one extra SQLite read (two
  marker lookups in one connection). `/people` still computes state for every
  identity before paginating, as before.
