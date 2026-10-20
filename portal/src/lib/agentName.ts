import { displayName } from "../app/lib/format";

/** The chat app's name for an agent, unless it is only the folder name. */
export function agentName(chatName: string | undefined, hubName: string, key: string): string {
  if (chatName && chatName !== key) return chatName;
  return displayName(hubName) || hubName || key;
}
