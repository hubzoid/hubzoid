// What the Get started cards (chat/GetStarted.tsx) remember on this page, per
// person and agent, and whether they may show at all.
import type { Agent, GetStarted, SessionUser } from "./types";

/** The last answer per person and agent, so a new chat shows it at once. */
export const shownCards = new Map<string, GetStarted>();
/** People and agents that answered 403 or 404: new chats don't ask again. */
export const refusedCards = new Set<string>();

/** Whose cards for which agent (`<user id>:<hub>`), or null when they can't
 *  show: the agent has none, or the Console doesn't open for this person
 *  (they manage no agent). */
export function getStartedKey(agent: Agent, user: SessionUser | null): string | null {
  if (!agent.get_started || user?.console === false) return null;
  return `${user?.id ?? "local"}:${agent.hub || agent.id}`;
}
