// Short confirmations ("Link copied.") as one small toast at the top right,
// with an optional action (Undo), and a screen-reader announcer for things
// that happen without focus moving (a reply starting and finishing).
import { useEffect, useSyncExternalStore } from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { t } from "../i18n/en";

type Toast = { id: number; text: string; tone: "success" | "error" | "info"; action?: { label: string; run: () => void } };
let toasts: Toast[] = [];
let next = 1;
const timers = new Map<number, ReturnType<typeof setTimeout>>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function toast(text: string, tone: Toast["tone"] = "success", action?: Toast["action"]) {
  const item = { id: next++, text, tone, action };
  // One at a time: a new confirmation replaces the last.
  toasts.forEach((x) => dismiss(x.id));
  toasts = [item];
  emit();
  arm(item);
}

function arm(item: Toast) {
  clearTimeout(timers.get(item.id));
  timers.set(item.id, setTimeout(() => dismiss(item.id), item.action ? 8000 : item.tone === "error" ? 7000 : 4000));
}

function dismiss(id: number) {
  clearTimeout(timers.get(id));
  timers.delete(id);
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
    <div className="hz-toaster" aria-live="polite" aria-relevant="additions">
      {items.map((item) => (
        <div
          key={item.id}
          role={item.tone === "error" ? "alert" : "status"}
          className="hz-toast"
          data-tone={item.tone}
          onMouseEnter={() => clearTimeout(timers.get(item.id))}
          onMouseLeave={() => arm(item)}
          onFocus={() => clearTimeout(timers.get(item.id))}
        >
          {item.tone === "error" ? (
            <XCircle size={15} className="flex-none text-danger" aria-hidden />
          ) : item.tone === "info" ? (
            <Info size={15} className="flex-none text-mute" aria-hidden />
          ) : (
            <CheckCircle2 size={15} className="flex-none text-success" aria-hidden />
          )}
          <span className="min-w-0 flex-1">{item.text}</span>
          {item.action && (
            <button
              type="button"
              className="hz-toast-action"
              onClick={() => {
                dismiss(item.id);
                item.action!.run();
              }}
            >
              {item.action.label}
            </button>
          )}
          <button type="button" className="hz-icon-btn !h-6 !w-6" aria-label={t.close} onClick={() => dismiss(item.id)}>
            <X size={13} aria-hidden />
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
