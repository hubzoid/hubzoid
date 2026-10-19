// Per-device sidebar choices: hidden or shown on wide screens, chats grouped by
// agent or listed by date, which agent groups are open, and the search dialog.
import { useSyncExternalStore } from "react";

type State = {
  collapsed: boolean;
  view: "agents" | "recent";
  open: Record<string, boolean>;
  showAll: Record<string, boolean>;
  searchOpen: boolean;
};

const KEY = "hz.sidebar";
function read(): State {
  const base: State = { collapsed: false, view: "agents", open: {}, showAll: {}, searchOpen: false };
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
    return {
      ...base,
      collapsed: raw.collapsed === true,
      view: raw.view === "recent" ? "recent" : "agents",
      open: raw.open && typeof raw.open === "object" ? raw.open : {},
    };
  } catch {
    return base;
  }
}

let state = read();
const listeners = new Set<() => void>();
function set(next: Partial<State>) {
  state = { ...state, ...next };
  try {
    localStorage.setItem(KEY, JSON.stringify({ collapsed: state.collapsed, view: state.view, open: state.open }));
  } catch {
    /* private mode: the choice lasts this visit */
  }
  listeners.forEach((l) => l());
}

export function useSidebarState(): State {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}

export const sidebar = {
  toggleCollapsed: () => set({ collapsed: !state.collapsed }),
  setView: (view: State["view"]) => set({ view }),
  /** An agent group is open if chosen so, else when it holds the chat on screen. */
  isOpen: (agent: string, fallback: boolean) => state.open[agent] ?? fallback,
  setOpen: (agent: string, open: boolean) => set({ open: { ...state.open, [agent]: open } }),
  toggleShowAll: (agent: string) => set({ showAll: { ...state.showAll, [agent]: !state.showAll[agent] } }),
  openSearch: () => set({ searchOpen: true }),
  closeSearch: () => set({ searchOpen: false }),
};
