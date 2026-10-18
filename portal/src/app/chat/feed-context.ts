// The conversation feed on screen, for components below the thread that act on
// the server directly (queue list, deletion preview).
import { createContext, useContext, useSyncExternalStore } from "react";
import type { ConversationFeed, DeletionPreview } from "./runtime";

export type FeedValue = {
  feed: ConversationFeed;
  confirmedDeletes: Map<string, DeletionPreview>;
  onError: (error: unknown) => void;
};

export const FeedContext = createContext<FeedValue | null>(null);

/** The feed and a re-render whenever it changes (null outside a chat page). */
export function useFeed(): FeedValue | null {
  const value = useContext(FeedContext);
  useSyncExternalStore(value ? value.feed.subscribe : noopSubscribe, value ? value.feed.getVersion : zero);
  return value;
}

const noopSubscribe = () => () => {};
const zero = () => 0;
