// The person's own organisation of chats (pins, folders, tags), from
// /api/chat/organization, kept in one small store the sidebar reads.
import { useSyncExternalStore } from "react";
import { del, enc, get, patch, post } from "./api";

export type Folder = { id: string; name: string; parent_id: string | null; position: number; revision: number };
export type Placement = {
  conversation_id: string;
  folder_id: string | null;
  pinned_position: number | null;
  archived: boolean;
  tag_ids: string[];
  revision: number;
};
export type Tag = { id: string; name: string; color: string; revision: number };
type State = { loaded: boolean; revision: number; folders: Folder[]; placements: Map<string, Placement>; tags: Tag[] };

let state: State = { loaded: false, revision: 0, folders: [], placements: new Map(), tags: [] };
const listeners = new Set<() => void>();
const set = (next: Partial<State>) => {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
};

export async function loadOrganization() {
  try {
    const org = await get<{ revision: number; folders: Folder[]; placements: Placement[]; tags: Tag[] }>("/api/chat/organization", {
      quiet401: true,
    });
    set({ loaded: true, revision: org.revision, folders: org.folders, tags: org.tags,
          placements: new Map(org.placements.map((p) => [p.conversation_id, p])) });
  } catch {
    set({ loaded: true });
  }
}

export function useOrganization(): State {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    () => state,
  );
}

/** Change one conversation's place (folder, pin, archive, tags): yours only. */
export async function organize(conversationId: string, change: Partial<Omit<Placement, "conversation_id" | "revision">>) {
  const cur = state.placements.get(conversationId);
  const out = await patch<Placement>(`/api/conversations/${enc(conversationId)}/organization`, {
    expected_revision: cur?.revision ?? 0,
    ...change,
  });
  const placements = new Map(state.placements);
  placements.set(conversationId, out);
  set({ placements });
  return out;
}

export async function createFolder(name: string) {
  const f = await post<Folder>("/api/chat/folders", { name });
  set({ folders: [...state.folders, f] });
  return f;
}

export async function deleteFolder(folder: Folder) {
  await del(`/api/chat/folders/${enc(folder.id)}`, { body: { expected_revision: folder.revision } });
  await loadOrganization();
}
