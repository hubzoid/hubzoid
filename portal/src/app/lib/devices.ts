// Devices (HUB-83): the offline shell worker, the connection banner, input
// preferences (send key, touch Enter, contrast, haptics) and keyboard
// shortcuts. Deployment defaults come from /api/chat/devices/settings; a
// person's own choices from their account preferences where allowed.
import { useEffect, useState, useSyncExternalStore } from "react";
import { get } from "./api";
import { accountPreference, usePresentation } from "../../lib/presentation";

export type DeviceSettings = {
  install: { enabled: boolean; display: string; start_path: string; share_target: boolean };
  offline: { shell_cache: boolean; navigation_timeout_ms: number };
  connection: { banner_delay_ms: number; resume_grace_ms: number; probe_max_interval_seconds: number };
  defaults: { send_key: "enter" | "mod_enter"; touch_enter: "newline" | "send"; follow_generation: boolean; scroll_on_branch_change: boolean; contrast: "system" | "standard" | "more"; haptics: boolean };
  allow_personal: Record<string, boolean>;
};

let settings: DeviceSettings | null = null;
const listeners = new Set<() => void>();

export async function loadDevices() {
  try {
    settings = await get<DeviceSettings>("/api/chat/devices/settings", { quiet401: true });
  } catch {
    settings = null;
  }
  listeners.forEach((l) => l());
  registerWorker();
}

export function useDevices(): DeviceSettings | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
    () => settings,
  );
}

function registerWorker() {
  if (!("serviceWorker" in navigator)) return;
  // The worker caches only the public shell; with shell_cache off it removes itself.
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

/** Sign-out: drop the cached shell so the next person starts clean. */
export function clearDeviceCache() {
  navigator.serviceWorker?.controller?.postMessage("hz:logout");
  // Unsent chat text kept for a sign-in that ended (Thread's draft) goes too.
  try {
    for (const k of Object.keys(sessionStorage)) if (k.startsWith("hz.draft")) sessionStorage.removeItem(k);
  } catch {
    /* no storage */
  }
}

export const isTouchOnly = typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches && !window.matchMedia?.("(pointer: fine)").matches;

/** An input preference: the person's own (when allowed), else the deployment's. */
export function useInputPreference<K extends keyof DeviceSettings["defaults"]>(key: K): DeviceSettings["defaults"][K] | undefined {
  const dev = useDevices();
  usePresentation();
  if (!dev) return undefined;
  const mine = dev.allow_personal[key] ? (accountPreference(key) as DeviceSettings["defaults"][K] | undefined) : undefined;
  return mine ?? dev.defaults[key];
}

/** How the composer sends: Enter, Mod+Enter, or (touch, newline) only the button. */
export function useSubmitMode(): "enter" | "ctrlEnter" | "none" {
  const sendKey = useInputPreference("send_key");
  const touchEnter = useInputPreference("touch_enter");
  if (isTouchOnly) return touchEnter === "send" ? "enter" : "none";
  return sendKey === "mod_enter" ? "ctrlEnter" : "enter";
}

/** The contrast overlay on <html data-contrast>. */
export function useContrast() {
  const contrast = useInputPreference("contrast");
  useEffect(() => {
    const more = contrast === "more" || (contrast === "system" && window.matchMedia?.("(prefers-contrast: more)").matches);
    document.documentElement.dataset.contrast = more ? "more" : "standard";
  }, [contrast]);
}

/** Online state, probed with backoff while the browser says offline or a probe fails. */
export function useConnection(): "online" | "offline" {
  const dev = useDevices();
  const [state, setState] = useState<"online" | "offline">("online");
  useEffect(() => {
    let stop = false;
    let failures = 0;
    let shown: ReturnType<typeof setTimeout> | null = null;
    const delay = dev?.connection.banner_delay_ms ?? 2000;
    const maxWait = (dev?.connection.probe_max_interval_seconds ?? 30) * 1000;
    const offline = () => {
      if (shown) return;
      shown = setTimeout(() => !stop && setState("offline"), delay);
    };
    const online = () => {
      if (shown) clearTimeout(shown);
      shown = null;
      failures = 0;
      setState("online");
    };
    const probe = async () => {
      if (stop) return;
      try {
        const r = await fetch("/api/chat/connection", { cache: "no-store", credentials: "same-origin" });
        if (r.ok) online();
        else throw new Error(String(r.status));
      } catch {
        failures++;
        offline();
      }
      if (!stop && failures) setTimeout(probe, Math.min(maxWait, 1000 * 2 ** failures));
    };
    const onOffline = () => {
      offline();
      void probe();
    };
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", () => void probe());
    if (!navigator.onLine) onOffline();
    return () => {
      stop = true;
      window.removeEventListener("offline", onOffline);
    };
  }, [dev]);
  return state;
}

/** Short vibration on completion, when the person turned haptics on. */
export function buzz() {
  try {
    if (accountPreference("haptics") === true) navigator.vibrate?.(30);
  } catch {
    /* not supported */
  }
}
