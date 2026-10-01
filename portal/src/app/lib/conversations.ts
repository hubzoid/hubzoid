// The sidebar's conversation list: one query at a time (search text and the
// archived view), paged with the server's cursor, and patched in place when a
// chat is created, renamed or titled so the list never waits for a refetch.
import { useSyncExternalStore } from "react";
import { del, enc, get, hubUrl, patch } from "./api";
import type { Conversation, ConversationPage } from "./types";

export type ListState = {
  items: Conversation[];
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  error: unknown;
  q: string;
  archived: boolean;
  loaded: boolean;
};

const PAGE = 50;

let state: ListState = {
  items: [],
  nextCursor: null,
  loading: false,
  loadingMore: false,
  error: null,
  q: "",
  archived: false,
  loaded: false,
};
const listeners = new Set<() => void>();
let generation = 0;
let controller: AbortController | null = null;

function set(next: Partial<ListState>) {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

const isArchived = (c: Conversation) => c.archived === true || c.archived === 1;

function url(q: string, archived: boolean, cursor?: string | null) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  params.set("archived", archived ? "1" : "0");
  params.set("limit", String(PAGE));
  if (cursor) params.set("cursor", cursor);
  return `/api/conversations?${params.toString()}`;
}

/** Load the first page for a query. `silent` keeps the current rows visible. */
export async function loadConversations(q = state.q, archived = state.archived, silent = false) {
  const mine = ++generation;
  controller?.abort();
  controller = new AbortController();
  const changed = q !== state.q || archived !== state.archived;
  set({ q, archived, loading: !silent || changed, error: null, ...(changed ? { items: [], nextCursor: null, loaded: false } : {}) });
  try {
    const page = await get<ConversationPage>(url(q, archived), { signal: controller.signal });
    if (mine !== generation) return;
    set({
      items: dedupe(page?.items ?? []),
      nextCursor: page?.next_cursor ?? null,
      loading: false,
      loaded: true,
    });
  } catch (error) {
    if (mine !== generation) return;
    if ((error as Error)?.name === "AbortError") return;
    set({ loading: false, error, loaded: true });
  }
}

export async function loadMore() {
  if (!state.nextCursor || state.loadingMore) return;
  const mine = generation;
  set({ loadingMore: true });
  try {
    const page = await get<ConversationPage>(url(state.q, state.archived, state.nextCursor));
    if (mine !== generation) return;
    set({
      items: dedupe([...state.items, ...(page?.items ?? [])]),
      nextCursor: page?.next_cursor ?? null,
      loadingMore: false,
    });
  } catch (error) {
    if (mine !== generation) return;
    set({ loadingMore: false, error });
  }
}

function dedupe(items: Conversation[]) {
  const seen = new Set<string>();
  return items.filter((c) => c && c.id && !seen.has(c.id) && seen.add(c.id));
}

/** A conversation appeared or changed on this device: show it at the top. */
export function upsertConversation(conversation: Conversation) {
  if (state.archived || state.q) {
    // Another view is open; it will refresh when the person returns.
    return;
  }
  const rest = state.items.filter((c) => c.id !== conversation.id);
  const existing = state.items.find((c) => c.id === conversation.id);
  set({ items: [{ ...existing, ...conversation }, ...rest] });
}

export function patchLocal(id: string, fields: Partial<Conversation>) {
  if (!state.items.some((c) => c.id === id)) return;
  set({ items: state.items.map((c) => (c.id === id ? { ...c, ...fields } : c)) });
}

export function removeLocal(id: string) {
  set({ items: state.items.filter((c) => c.id !== id) });
}

export function findConversation(id: string): Conversation | undefined {
  return state.items.find((c) => c.id === id);
}

export async function renameConversation(id: string, title: string) {
  const res = await patch<{ conversation?: Conversation }>(`/api/conversations/${enc(id)}`, { title });
  patchLocal(id, { title, ...(res?.conversation ?? {}) });
}

export async function setArchived(id: string, archived: boolean) {
  await patch(`/api/conversations/${enc(id)}`, { archived });
  // Leaving the current view either way.
  if (isArchived({ archived } as Conversation) !== state.archived) removeLocal(id);
}

/** DELETE is hub-scoped: it removes the chat's files on that hub too. */
export async function deleteConversation(conversation: Pick<Conversation, "id" | "api_base">) {
  await del(hubUrl(conversation.api_base, `/api/conversations/${enc(conversation.id)}`));
  removeLocal(conversation.id);
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export function useConversations(): ListState {
  return useSyncExternalStore(subscribe, () => state);
}

export function getListState() {
  return state;
}
