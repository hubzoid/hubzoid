// Which agent a new chat starts with, and the agents this browser chatted with
// most recently. A per-viewer convenience only: storage may be unavailable, and
// then nothing is remembered.
const RECENT_KEY = "hz-recent-agents";
// The agent a new chat opens with: the last one shown or picked.
const LAST_AGENT_KEY = "hz-last-agent";
const KEEP = 5;

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Agents this browser picked or started a chat with, most recent first. */
export function recentAgents(): string[] {
  try {
    const stored: unknown = JSON.parse(read(RECENT_KEY) ?? "[]");
    return Array.isArray(stored) ? stored.filter((x): x is string => typeof x === "string").slice(0, KEEP) : [];
  } catch {
    return [];
  }
}

export function lastAgent(): string | null {
  return read(LAST_AGENT_KEY);
}

/** The agent on screen in a new chat; a new chat opens with it next time. */
export function rememberLast(id: string) {
  try {
    localStorage.setItem(LAST_AGENT_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

/** The person picked this agent or started a chat with it. */
export function rememberAgent(id: string) {
  rememberLast(id);
  try {
    const ids = [id, ...recentAgents().filter((x) => x !== id)].slice(0, KEEP);
    localStorage.setItem(RECENT_KEY, JSON.stringify(ids));
  } catch {
    /* storage unavailable */
  }
}
