// Light, dark or match-system, remembered per device. Uses the same storage key
// and <html data-theme> attribute as the Console, so a choice made in one
// carries to the other. Storage can be unavailable (private windows, blocked
// site data); the theme then simply follows the system for this visit.
import { useEffect, useSyncExternalStore } from "react";

export type ThemeMode = "light" | "dark" | "system";
const KEY = "hz-theme";
const EVENT = "hz:theme";

function readMode(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* storage unavailable */
  }
  return memoryMode;
}

let memoryMode: ThemeMode = "system";

const media = () =>
  typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function subscribe(callback: () => void) {
  const mq = media();
  mq?.addEventListener("change", callback);
  window.addEventListener(EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    mq?.removeEventListener("change", callback);
    window.removeEventListener(EVENT, callback);
    window.removeEventListener("storage", callback);
  };
}

export function resolveTheme(mode: ThemeMode): "light" | "dark" {
  if (mode === "system") return media()?.matches ? "dark" : "light";
  return mode;
}

/** Apply the theme to <html> right away (before React renders). */
export function applyTheme(mode: ThemeMode = readMode()) {
  const resolved = resolveTheme(mode);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
}

export function setThemeMode(mode: ThemeMode) {
  memoryMode = mode;
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    /* storage unavailable: keep it for this visit */
  }
  applyTheme(mode);
  window.dispatchEvent(new Event(EVENT));
}

export function useTheme(): { mode: ThemeMode; resolved: "light" | "dark"; setMode: (m: ThemeMode) => void } {
  const mode = useSyncExternalStore(subscribe, readMode);
  const resolved = useSyncExternalStore(subscribe, () => resolveTheme(readMode()));
  useEffect(() => {
    applyTheme(mode);
  }, [mode, resolved]);
  return { mode, resolved, setMode: setThemeMode };
}
