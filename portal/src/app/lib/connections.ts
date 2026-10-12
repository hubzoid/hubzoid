import type { Connection } from "./types";

/** How one connection reads to its person, in the chat app and the Console. */
export type Kind = "connected" | "attention" | "off" | "blocked" | "shared";

export function kindOf(c: Connection): Kind {
  if (c.allowed === false) return "blocked";
  if (c.auth_type === "shared") return "shared";
  const status = (c.status || "").toLowerCase();
  if (c.connected && ["", "ok", "active", "connected", "valid"].includes(status)) return "connected";
  if (c.connected || ["expired", "error", "revoked", "invalid", "needs_reauth", "reauth", "failed"].includes(status))
    return "attention";
  return "off";
}
