# Portal account-state contract

How `/portal/api/access`, `/portal/api/people` and `POST /portal/api/people/block`
describe whether a person can use a hub, and *why* they can't when they can't.

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

### `GET /portal/api/access?hub=…`

Each element of `rows[]` now carries `suspended`, `account_unavailable`,
`blocked` and the corrected `status` alongside the existing fields (`subject`,
`display`, `kind`, `perms`, `inherited`, `effective`, `center`).

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
  `_meta_get` helper (portal.py `_account_flags`). No public store method exposes
  them separately yet. If the store adds one, switch the helper to it.
- "Missing from directory" and "pending approval" both set
  `account_unavailable`; they are told apart only by the identity's `pending`
  column, which `reconcile_accounts` does not reset for a vanished account. A
  user who was pending and then deleted keeps `status=pending-approval`.
- No batch or atomic multi-grant save exists; the staged Access editor still
  issues one request per change (see the backend contract review, A2/A6).
- Reactivate does not restore the grants that blocking deleted.
- Each `/access` row and `/people` entry now costs one extra SQLite read (two
  marker lookups in one connection). `/people` still computes state for every
  identity before paginating, as before.
