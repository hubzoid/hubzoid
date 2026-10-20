// The notices inbox as one shared store: the bell lists them, and the sidebar
// marks chats with an unread reply. Polled while the app is open; desktop and
// sound alerts stay opt-in (account preferences) and only fire while the tab
// is hidden.
import { useSyncExternalStore } from "react";
import { t } from "../i18n/en";
import { get, patch } from "./api";

export type Notice = {
  id: string;
  state: string;
  kind: string;
  conversation_id: string;
  title: string | null;
  link: string;
  created_at: number;
  /** The hub the reply came from. */
  hub?: string | null;
};

const POLL_MS = 20000;
let items: Notice[] = [];
let started = false;
const listeners = new Set<() => void>();
const shown = new Set<string>();
const prefs = { desktop: false, sound: false };
const emit = () => listeners.forEach((l) => l());

export function noticeLabel(kind: string) {
  return kind === "run.failed" ? t.notices.failed : kind === "run.interrupted" ? t.notices.interrupted : t.notices.completed;
}

function chime() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.06, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.25);
  } catch {
    /* no audio available */
  }
}

async function load() {
  try {
    const r = await get<{ items: Notice[] }>("/api/chat/notifications", { quiet401: true });
    items = r.items;
    emit();
    for (const item of r.items) {
      if (item.state !== "unread" || shown.has(item.id)) continue;
      shown.add(item.id);
      if (!document.hidden) continue;
      if (prefs.desktop && "Notification" in window && Notification.permission === "granted")
        new Notification(noticeLabel(item.kind), { body: item.title || t.sidebar.untitled, tag: item.id });
      if (prefs.sound) chime();
    }
  } catch {
    /* the inbox is optional; the chat works without it */
  }
}

function start() {
  if (started) return;
  started = true;
  get<{ values: Record<string, unknown> }>("/api/chat/preferences", { quiet401: true })
    .then((p) => {
      prefs.desktop = p.values["notifications.desktop"] === true;
      prefs.sound = p.values["notifications.sound"] === true;
    })
    .catch(() => {});
  void load();
  setInterval(() => void load(), POLL_MS);
  window.addEventListener("focus", () => void load());
}

export function useNotices(): Notice[] {
  start();
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => items,
  );
}

export async function markNotice(id: string, state: "read" | "dismissed") {
  await patch(`/api/chat/notifications/${encodeURIComponent(id)}`, { state });
  items = state === "dismissed" ? items.filter((i) => i.id !== id) : items.map((i) => (i.id === id ? { ...i, state } : i));
  emit();
}

export async function markAllRead() {
  const unread = items.filter((i) => i.state === "unread");
  await Promise.all(unread.map((i) => markNotice(i.id, "read").catch(() => {})));
}

/** Chats with an unread reply, for the sidebar's dot. */
export function unreadChats(list: Notice[]): Set<string> {
  return new Set(list.filter((i) => i.state === "unread" && i.conversation_id).map((i) => i.conversation_id));
}

/** Opening a chat reads its notices. */
export function readConversation(conversationId: string) {
  start();
  const mine = items.filter((i) => i.conversation_id === conversationId && i.state === "unread");
  for (const i of mine) void markNotice(i.id, "read").catch(() => {});
}
