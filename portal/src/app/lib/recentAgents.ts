// The agents this browser started chats with, most recent first. A per-viewer
// convenience only: storage may be unavailable, and then nothing is remembered.
const RECENT_KEY = "hz-recent-agents";
// Read before the recent list existed, so a returning person keeps their agent.
const LAST_AGENT_KEY = "hz-last-agent";
const KEEP = 5;

export function recentAgents(): string[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    const ids = Array.isArray(stored) ? stored.filter((x): x is string => typeof x === "string") : [];
    if (ids.length) return ids.slice(0, KEEP);
    const last = localStorage.getItem(LAST_AGENT_KEY);
    return last ? [last] : [];
  } catch {
    return [];
  }
}

export function lastAgent(): string | null {
  return recentAgents()[0] ?? null;
}

export function rememberAgent(id: string) {
  try {
    const ids = [id, ...recentAgents().filter((x) => x !== id)].slice(0, KEEP);
    localStorage.setItem(RECENT_KEY, JSON.stringify(ids));
    localStorage.setItem(LAST_AGENT_KEY, id);
  } catch {
    /* storage unavailable */
  }
}
