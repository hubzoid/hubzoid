// Short confirmations ("Link copied.") and a screen-reader announcer for
// things that happen without focus moving (a reply starting and finishing).
import { useEffect, useSyncExternalStore } from "react";
import { CheckCircle2, X, XCircle } from "lucide-react";
import { t } from "../i18n/en";

type Toast = { id: number; text: string; tone: "success" | "error" | "info" };
let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function toast(text: string, tone: Toast["tone"] = "success") {
  const item = { id: next++, text, tone };
  toasts = [...toasts.slice(-2), item];
  emit();
  setTimeout(() => dismiss(item.id), tone === "error" ? 7000 : 3500);
}

function dismiss(id: number) {
  toasts = toasts.filter((x) => x.id !== id);
  emit();
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export function Toaster() {
  const items = useSyncExternalStore(subscribe, () => toasts);
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-4 z-[80] flex flex-col items-center gap-2 px-4"
      aria-live="polite"
      aria-relevant="additions"
    >
      {items.map((item) => (
        <div
          key={item.id}
          role={item.tone === "error" ? "alert" : "status"}
          className="pointer-events-auto flex max-w-[min(92vw,440px)] items-center gap-2.5 rounded-xl border border-line bg-ink py-2.5 pl-3.5 pr-2 text-sm text-bg"
        >
          {item.tone === "error" ? (
            <XCircle size={16} className="flex-none text-[#ffb4b4] dark:text-danger-solid" aria-hidden />
          ) : (
            <CheckCircle2 size={16} className="flex-none text-[#a1d8b6] dark:text-success" aria-hidden />
          )}
          <span className="min-w-0 flex-1">{item.text}</span>
          <button
            type="button"
            className="flex-none rounded-md p-1 opacity-70 hover:opacity-100"
            aria-label={t.close}
            onClick={() => dismiss(item.id)}
          >
            <X size={14} aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}

// ---- announcer ---------------------------------------------------------------
let announcement = "";
const announceListeners = new Set<() => void>();

/** Say something to screen readers without moving focus. */
export function announce(text: string) {
  // Clear first so repeating the same sentence is announced again.
  announcement = "";
  announceListeners.forEach((l) => l());
  setTimeout(() => {
    announcement = text;
    announceListeners.forEach((l) => l());
  }, 60);
}

export function LiveRegion() {
  const text = useSyncExternalStore(
    (l) => {
      announceListeners.add(l);
      return () => announceListeners.delete(l);
    },
    () => announcement,
  );
  useEffect(() => () => void (announcement = ""), []);
  return (
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid="live-region">
      {text}
    </div>
  );
}
