// The chat app's view of the shared presentation state (src/lib/presentation):
// light, dark, OLED or match-system, plus text size and density, resolved from
// the deployment's limits, the person's account preference and this device.
import { apply, setMode as setPresentationMode, usePresentation, type Appearance } from "../../lib/presentation";

export type ThemeMode = Appearance;

/** Apply the device's last choice to <html> right away (before React renders). */
export function applyTheme() {
  apply();
}

export function setThemeMode(mode: ThemeMode) {
  setPresentationMode(mode);
}

export function useTheme(): {
  mode: ThemeMode;
  resolved: "light" | "dark";
  setMode: (m: ThemeMode) => void;
  allowed: ThemeMode[];
  locked: boolean;
} {
  const view = usePresentation();
  return {
    mode: view.mode,
    resolved: view.dark ? "dark" : "light",
    setMode: setPresentationMode,
    allowed: view.allowed.modes,
    locked: view.locked.mode,
  };
}
