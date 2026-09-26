import type { Access, AccessRow, Permission } from "../../api";
import { EVERYONE, MANAGE_ACCESS, USE_HUB } from "../../lib/format";

/**
 * Pure staging logic for the Access editor. Nothing here talks to the server;
 * it only decides what a draft means and which requests a save would send.
 *
 * Backend rules this mirrors (hubzoid/access/store.py, hubzoid/access/service.py):
 *  - granting any capability auto-grants `use_hub`;
 *  - revoking `use_hub` cascades and removes every direct grant in the agent;
 *  - the staged operations are saved together in one atomic request;
 *  - an agent administrator (not organization-wide) grants and removes only
 *    what they hold themselves (`grantable`), never their own access or an
 *    organization administrator's, and never an organization-admin-only
 *    capability (`delegate_grantable: false`);
 *  - an `included` capability comes with `use_hub` and is never granted; an
 *    obsolete grant can be removed, never granted.
 */
export type Operation = { action: "grant" | "revoke"; permission: string };

export type Diff = { added: string[]; removed: string[] };

export function diff(before: string[], after: string[]): Diff {
  return {
    added: after.filter((p) => !before.includes(p)),
    removed: before.filter((p) => !after.includes(p)),
  };
}

/** The minimal ordered request list that turns `before` into `after`. */
export function planOperations(before: string[], after: string[]): Operation[] {
  const { added, removed } = diff(before, after);
  if (removed.includes(USE_HUB)) {
    // One request: the server-side cascade removes every other grant, so
    // sending the individual revokes would only add failure points.
    return [
      { action: "revoke", permission: USE_HUB },
      ...added.map((permission) => ({ action: "grant" as const, permission })),
    ];
  }
  const grants = added.filter(
    // `use_hub` is implied by any other grant; send it only when it is the
    // sole change.
    (p) => p !== USE_HUB || added.length === 1,
  );
  return [
    ...removed.map((permission) => ({ action: "revoke" as const, permission })),
    ...grants.map((permission) => ({ action: "grant" as const, permission })),
  ];
}

/** Toggle a capability in the draft, keeping the implication rule intact. */
export function toggle(selected: string[], permission: string, on: boolean) {
  if (on) {
    const next = new Set(selected);
    next.add(permission);
    next.add(USE_HUB);
    return [...next];
  }
  if (permission === USE_HUB) return [];
  return selected.filter((p) => p !== permission);
}

export type Lock = {
  reason: string;
  label?: "Inherited" | "Required" | "Outside your access" | "Admins only" | "Included" | "No longer available";
} | null;

type Viewer = Pick<Access, "can_manage_admins" | "grantable" | "viewer">;

/**
 * Why a checkbox cannot be changed, or null when it can. The backend enforces
 * the same rules; the UI explains them up front instead of failing on save.
 */
export function lockFor(
  permission: string,
  row: AccessRow,
  access: Viewer,
  selected: string[],
  meta?: Permission,
): Lock {
  if (meta?.default === "included")
    return { label: "Included", reason: "Comes with Use this agent; there is nothing to grant separately." };
  // A blocked (admin-suspended) or unavailable (chat account gone) person can have
  // EXISTING grants removed — offboarding — but must not receive NEW ones. So lock
  // only a permission they do not already hold; leave held ones unlockable so they
  // can be unchecked (revoked).
  if ((row.suspended || row.account_unavailable) && !row.perms.includes(permission))
    return {
      reason: row.suspended
        ? "Blocked by an administrator — reactivate them under People to grant new access. Existing access can still be removed."
        : "Their chat account is unavailable, so new access can't be added — but existing access can be removed.",
    };
  if (meta?.obsolete && !row.perms.includes(permission))
    return { label: "No longer available", reason: "This capability no longer exists, so it can’t be granted." };
  if (row.subject === EVERYONE)
    return { reason: "“Everyone signed in” can only be removed, from the access list." };
  if (row.inherited.includes(permission))
    return {
      label: "Inherited",
      reason:
        "Held through organization administrator rights. Change it under People.",
    };
  if (
    !access.can_manage_admins &&
    (permission === MANAGE_ACCESS ||
      (permission === USE_HUB && row.effective.includes(MANAGE_ACCESS)))
  )
    return { reason: "Only organization administrators can change this." };
  if (!access.can_manage_admins && access.viewer && row.subject === access.viewer)
    return { reason: "You can’t change your own access. Ask an organization administrator." };
  if (!access.can_manage_admins && row.inherited.includes(MANAGE_ACCESS))
    return { reason: "Only organization administrators can change an organization administrator’s access." };
  if (!access.can_manage_admins && meta?.delegate_grantable === false)
    return {
      label: "Admins only",
      reason: "Only organization administrators can grant or remove this.",
    };
  if (!access.can_manage_admins && access.grantable) {
    const ceiling = access.grantable;
    if (!ceiling.includes(permission))
      return {
        label: "Outside your access",
        reason: "You can only grant or remove capabilities you hold in this agent yourself.",
      };
    if (permission === USE_HUB && row.perms.some((p) => p !== USE_HUB && !ceiling.includes(p)))
      return {
        label: "Outside your access",
        reason: "They hold capabilities you don’t, so removing all their access here needs an organization administrator.",
      };
  }
  if (permission === USE_HUB && selected.some((p) => p !== USE_HUB))
    return {
      label: "Required",
      reason:
        "Required by the other selected capabilities. Use “Remove all access” to take everything away.",
    };
  return null;
}

/** Whether "Remove all access" is available for this row. */
export function canRemoveAll(
  row: AccessRow,
  access: Viewer,
) {
  // Available whenever there are direct grants to remove — including for a blocked or
  // unavailable account, so retained access can be offboarded. `lockFor` returns null
  // for a held permission even when blocked, so this stays true in that case.
  return (
    row.perms.length > 0 &&
    row.subject !== EVERYONE &&
    lockFor(USE_HUB, row, access, []) === null
  );
}

/** Capabilities in catalog order, `use_hub` first, then alphabetical. */
export function orderCapabilities(perms: string[], catalogOrder: string[]) {
  const rank = (p: string) =>
    p === USE_HUB ? -1 : Math.max(catalogOrder.indexOf(p), catalogOrder.length);
  return [...perms].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

export function emptyRow(subject = ""): AccessRow {
  return {
    subject,
    display: "",
    kind: subject.startsWith("workflow:") ? "service" : "person",
    status: subject.startsWith("workflow:") ? "service" : "awaiting-signup",
    perms: [],
    inherited: [],
    effective: [],
  };
}

/** Everything the drawer needs to stage one person's change set. */
export type Draft = {
  mode: "add" | "edit";
  /** What the server currently holds (empty for a new person). */
  row: AccessRow;
  /** Identity being edited; editable only in add mode. */
  subject: string;
  /** Direct capabilities the draft wants. */
  selected: string[];
  step: "edit" | "review" | "saving" | "failed";
  /** Requests the review step promised to send, in order. */
  operations: Operation[];
  /** How many of `operations` completed. */
  progress: number;
  failure?: string;
  /** A failed save whose outcome we couldn't confirm (network/5xx), as opposed
   *  to a definite client rejection (4xx) that committed nothing. */
  uncertain?: boolean;
  notice?: string;
};

export function draftFor(row?: AccessRow): Draft {
  return {
    mode: row ? "edit" : "add",
    row: row ?? emptyRow(),
    subject: row?.subject ?? "",
    selected: row ? [...row.perms] : [USE_HUB],
    step: "edit",
    operations: [],
    progress: 0,
  };
}
