// The notices inbox: replies that completed, failed or were interrupted while
// the person was elsewhere. Desktop and sound alerts are opt-in (account
// preferences), shown once per notice, and only while the tab is hidden.
import { useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { Popover } from "radix-ui";
import { t } from "../i18n/en";
import { get, patch } from "../lib/api";
import { linkClick } from "../lib/router";

type Item = { id: string; state: string; kind: string; conversation_id: string; title: string | null; link: string; created_at: number };

const POLL_MS = 20000;
const shown = new Set<string>();

function label(kind: string) {
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

export function NotificationsButton() {
  const [items, setItems] = useState<Item[]>([]);
  const [open, setOpen] = useState(false);
  const prefs = useRef<{ desktop: boolean; sound: boolean }>({ desktop: false, sound: false });

  useEffect(() => {
    let stop = false;
    get<{ values: Record<string, unknown> }>("/api/chat/preferences", { quiet401: true })
      .then((p) => {
        prefs.current = { desktop: p.values["notifications.desktop"] === true, sound: p.values["notifications.sound"] === true };
      })
      .catch(() => {});
    const load = () =>
      get<{ items: Item[] }>("/api/chat/notifications", { quiet401: true })
        .then((r) => {
          if (stop) return;
          setItems(r.items);
          for (const item of r.items) {
            if (item.state !== "unread" || shown.has(item.id)) continue;
            shown.add(item.id);
            if (!document.hidden) continue;
            if (prefs.current.desktop && "Notification" in window && Notification.permission === "granted")
              new Notification(label(item.kind), { body: item.title || t.sidebar.untitled, tag: item.id });
            if (prefs.current.sound) chime();
          }
        })
        .catch(() => {});
    void load();
    const timer = setInterval(load, POLL_MS);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      stop = true;
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  const unread = items.filter((i) => i.state === "unread").length;
  const mark = (item: Item, state: "read" | "dismissed") =>
    patch(`/api/chat/notifications/${encodeURIComponent(item.id)}`, { state })
      .then(() => setItems((all) => (state === "dismissed" ? all.filter((i) => i.id !== item.id) : all.map((i) => (i.id === item.id ? { ...i, state } : i)))))
      .catch(() => {});

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="mb-1 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] text-mute hover:bg-hover hover:text-ink"
          aria-label={unread ? t.notices.unread(unread) : t.notices.title}
          data-testid="notifications"
        >
          <Bell size={15} aria-hidden />
          {t.notices.title}
          {unread > 0 && <span className="hz-badge ml-auto">{unread}</span>}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="hz-menu w-80 !p-1" side="top" align="start" sideOffset={6}>
          {items.length === 0 ? (
            <p className="m-0 px-3 py-3 text-[13px] text-mute">{t.notices.empty}</p>
          ) : (
            <ul className="m-0 max-h-80 list-none overflow-y-auto p-0">
              {items.map((item) => (
                <li key={item.id} className="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-hover" data-state={item.state}>
                  <a
                    href={item.link}
                    className="min-w-0 flex-1"
                    onClick={(e) => {
                      void mark(item, "read");
                      setOpen(false);
                      linkClick(e, item.link);
                    }}
                  >
                    <span className="block truncate text-[13px] text-ink">{item.title || t.sidebar.untitled}</span>
                    <span className="block text-[12px] text-mute">
                      {item.state === "unread" && <span className="hz-dot mr-1.5" aria-hidden />}
                      {label(item.kind)}
                    </span>
                  </a>
                  <button type="button" className="text-[12px] text-mute hover:text-ink" onClick={() => void mark(item, "dismissed")}>
                    {t.notices.dismiss}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
