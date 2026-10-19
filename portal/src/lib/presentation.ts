// One presentation state for the chat app and the Console: the deployment's
// theme revision (GET /api/chat/theme), its presentation defaults (GET
// /api/chat/bootstrap) and the signed-in person's preferences (GET/PUT
// /api/chat/preferences). Both surfaces render from the same resolved tokens,
// so the Console and chat never disagree and choosing an agent never recolours
// the application.
//
// Precedence for appearance, text size and density: a deployment lock wins,
// then the person's account preference, then this device's choice, then the
// deployment default. Preferences only change how things look: never who may
// approve anything or how anyone signs in.
//
// Storage can be unavailable (private windows, blocked site data): the device
// choice then lasts for this visit only.

import { useSyncExternalStore } from "react";

export type Appearance = "light" | "dark" | "oled" | "system";
export type Density = "comfortable" | "compact";
export type Palette = Record<string, string>;

export type ThemePayload = {
  revision: string;
  palettes: Record<"light" | "dark" | "oled", Palette>;
  typography: { fontFamily: string; monoFamily: string; baseSize: number; lineHeight: number };
  shape: { controlRadius: number; cardRadius: number };
  css: Record<"light" | "dark" | "oled", Record<string, string>>;
  fonts: { sans: string; mono: string };
};

export type Bootstrap = {
  config_revision: number;
  theme_revision: string;
  appearance: {
    default_mode: Appearance;
    allowed_modes: Appearance[];
    default_density: Density;
    allowed_densities: Density[];
    default_text_scale: number;
    allowed_text_scales: number[];
  };
  preferences: { mode: boolean; density: boolean; text_scale: boolean; timezone: boolean };
  timezone: string;
};

type Values = { mode?: Appearance; density?: Density; text_scale?: number; timezone?: string };

type State = {
  theme: ThemePayload | null;
  bootstrap: Bootstrap | null;
  account: { revision: number; values: Values } | null;
  device: Values;
  saving: boolean;
  saveError: string | null;
  systemDark: boolean;
};

const DEVICE_KEY = "hz-theme"; // mode only, kept from 1.x so a device choice carries over
const DEVICE_PREFS_KEY = "hz-presentation";

function readDevice(): Values {
  const out: Values = {};
  try {
    const mode = localStorage.getItem(DEVICE_KEY);
    if (mode === "light" || mode === "dark" || mode === "oled" || mode === "system") out.mode = mode;
    const raw = localStorage.getItem(DEVICE_PREFS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed.density === "comfortable" || parsed.density === "compact") out.density = parsed.density;
      if (typeof parsed.text_scale === "number") out.text_scale = parsed.text_scale;
    }
  } catch {
    /* storage unavailable */
  }
  return out;
}

function writeDevice(values: Values) {
  try {
    if (values.mode) localStorage.setItem(DEVICE_KEY, values.mode);
    localStorage.setItem(
      DEVICE_PREFS_KEY,
      JSON.stringify({ density: values.density, text_scale: values.text_scale }),
    );
  } catch {
    /* storage unavailable: kept for this visit */
  }
}

const media = () =>
  typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

let state: State = {
  theme: null,
  bootstrap: null,
  account: null,
  device: typeof window !== "undefined" ? readDevice() : {},
  saving: false,
  saveError: null,
  systemDark: !!media()?.matches,
};
const listeners = new Set<() => void>();

function set(patch: Partial<State>) {
  state = { ...state, ...patch };
  apply();
  listeners.forEach((l) => l());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

if (typeof window !== "undefined") {
  media()?.addEventListener("change", (e) => set({ systemDark: e.matches }));
  window.addEventListener("storage", (e) => {
    if (e.key === DEVICE_KEY || e.key === DEVICE_PREFS_KEY) set({ device: readDevice() });
  });
}

export type Effective = {
  mode: Appearance;
  resolved: "light" | "dark" | "oled";
  dark: boolean;
  density: Density;
  textScale: number;
  locked: { mode: boolean; density: boolean; text_scale: boolean };
  allowed: { modes: Appearance[]; densities: Density[]; textScales: number[] };
  source: { mode: string; density: string; text_scale: string };
};

function pick<T>(
  key: "mode" | "density" | "text_scale",
  deploymentDefault: T,
  allowed: T[],
  locked: boolean,
): [T, string] {
  if (!locked) {
    const account = state.account?.values[key] as T | undefined;
    if (account !== undefined && allowed.includes(account)) return [account, "account"];
    const device = state.device[key] as T | undefined;
    if (device !== undefined && allowed.includes(device)) return [device, "device"];
  }
  return [deploymentDefault, state.bootstrap ? "deployment" : "default"];
}

export function effective(): Effective {
  const b = state.bootstrap;
  const modes: Appearance[] = b?.appearance.allowed_modes ?? ["light", "dark", "oled", "system"];
  const densities: Density[] = b?.appearance.allowed_densities ?? ["comfortable", "compact"];
  const scales = b?.appearance.allowed_text_scales ?? [1, 1.125, 1.25, 1.5];
  const locked = {
    mode: b ? !b.preferences.mode : false,
    density: b ? !b.preferences.density : false,
    text_scale: b ? !b.preferences.text_scale : false,
  };
  const [mode, modeSource] = pick("mode", b?.appearance.default_mode ?? "system", modes, locked.mode);
  const [density, densitySource] = pick("density", b?.appearance.default_density ?? "comfortable", densities, locked.density);
  const [textScale, scaleSource] = pick("text_scale", b?.appearance.default_text_scale ?? 1, scales, locked.text_scale);
  const resolved = mode === "system" ? (state.systemDark ? "dark" : "light") : mode;
  return {
    mode,
    resolved,
    dark: resolved !== "light",
    density,
    textScale,
    locked,
    allowed: { modes, densities, textScales: scales },
    source: { mode: modeSource, density: densitySource, text_scale: scaleSource },
  };
}

let appliedVars: string[] = [];

/** Put the effective presentation on <html>: data-theme (light|dark, which the
 *  existing CSS keys on), data-appearance (adds oled), data-density, the
 *  theme revision's CSS variables and the root font size for text scale. */
export function apply() {
  if (typeof document === "undefined") return;
  const e = effective();
  const root = document.documentElement;
  root.dataset.theme = e.dark ? "dark" : "light";
  root.dataset.appearance = e.resolved;
  root.dataset.density = e.density;
  root.style.colorScheme = e.dark ? "dark" : "light";
  const vars = state.theme?.css?.[e.resolved];
  // Clear variables a previous revision set that this one doesn't, so a
  // rollback never leaves a stale colour behind.
  for (const name of appliedVars) if (!vars || !(name in vars)) root.style.removeProperty(name);
  appliedVars = vars ? Object.keys(vars) : [];
  if (vars) for (const [name, value] of Object.entries(vars)) root.style.setProperty(name, value);
  const base = state.theme?.typography.baseSize ?? 16;
  root.style.fontSize = `${Math.round(base * e.textScale * 100) / 100}px`;
  if (state.theme) root.dataset.themeRevision = state.theme.revision;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    let message = `${response.status}`;
    try {
      const data = await response.json();
      message = data?.detail?.message || message;
    } catch {
      /* not JSON */
    }
    const error = new Error(message) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return (await response.json()) as T;
}

let publicLoad: Promise<void> | null = null;

/** Load the public theme and defaults once (both are public, pre-sign-in). */
export function loadPublic(force = false): Promise<void> {
  if (publicLoad && !force) return publicLoad;
  publicLoad = Promise.all([
    json<ThemePayload>("/api/chat/theme").catch(() => null),
    json<Bootstrap>("/api/chat/bootstrap").catch(() => null),
  ]).then(([theme, bootstrap]) => {
    set({ theme: theme ?? state.theme, bootstrap: bootstrap ?? state.bootstrap });
  });
  return publicLoad;
}

/** After sign-in: this person's account preferences (they win over the device). */
export async function loadAccount(): Promise<void> {
  try {
    const prefs = await json<{ revision: number; values: Values }>("/api/chat/preferences");
    set({ account: { revision: prefs.revision, values: prefs.values ?? {} } });
  } catch {
    /* signed out or unavailable: the device choice stands */
  }
}

/** Sign-out: forget the account layer (public defaults and the device stay). */
export function clearAccount() {
  set({ account: null, saveError: null });
}

/** Re-read the theme after a branding activation, without a page reload. */
export async function refreshTheme(): Promise<void> {
  await loadPublic(true);
}

async function saveAccount(values: Values, retry = true): Promise<void> {
  if (!state.account) return;
  set({ saving: true, saveError: null });
  try {
    const result = await json<{ revision: number; values: Values }>("/api/chat/preferences", {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ expected_revision: state.account.revision, values }),
    });
    set({ account: { revision: result.revision, values: result.values }, saving: false });
  } catch (error) {
    const status = (error as { status?: number }).status;
    if (status === 409 && retry) {
      await loadAccount();
      return saveAccount({ ...state.account?.values, ...values }, false);
    }
    set({ saving: false, saveError: (error as Error).message });
  }
}

/** A feature's own account preference ("notifications.desktop"), merged into
 *  the same preference document as appearance. */
export function setAccountPreference(key: string, value: unknown) {
  if (state.account) void saveAccount({ ...state.account.values, [key]: value } as Values);
}

export function accountPreference(key: string): unknown {
  return (state.account?.values as Record<string, unknown> | undefined)?.[key];
}

function change(key: "mode" | "density" | "text_scale", value: unknown) {
  const device = { ...state.device, [key]: value } as Values;
  writeDevice(device);
  set({ device });
  if (state.account) void saveAccount({ ...state.account.values, [key]: value });
}

export const setMode = (mode: Appearance) => change("mode", mode);
export const setDensity = (density: Density) => change("density", density);
export const setTextScale = (scale: number) => change("text_scale", scale);

export type PresentationView = Effective & {
  theme: ThemePayload | null;
  saving: boolean;
  saveError: string | null;
  signedIn: boolean;
};

let cached: { key: State; view: PresentationView } | null = null;

function snapshot(): PresentationView {
  if (cached && cached.key === state) return cached.view;
  const view = {
    ...effective(),
    theme: state.theme,
    saving: state.saving,
    saveError: state.saveError,
    signedIn: !!state.account,
  };
  cached = { key: state, view };
  return view;
}

export function usePresentation(): PresentationView {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/** Ant Design tokens from the active palette, so the Console renders the
 *  same revision as the chat. */
export function antdTokens(view: PresentationView): Record<string, string | number> | null {
  const palette = view.theme?.palettes?.[view.resolved];
  if (!palette) return null;
  const shape = view.theme!.shape;
  return {
    colorPrimary: palette.action,
    colorPrimaryHover: palette.actionHover,
    colorPrimaryActive: palette.actionActive,
    colorTextLightSolid: palette.actionText,
    colorLink: palette.link,
    colorLinkHover: palette.actionHover,
    colorText: palette.text,
    colorTextSecondary: palette.mutedText,
    colorBorder: palette.border,
    colorBorderSecondary: palette.borderSoft,
    colorBgLayout: palette.surface,
    colorBgContainer: palette.elevated,
    colorBgElevated: palette.elevated,
    colorSuccessText: palette.successText,
    colorSuccessBg: palette.successBackground,
    colorSuccessBorder: palette.successBorder,
    colorWarningText: palette.warningText,
    colorWarningBg: palette.warningBackground,
    colorWarningBorder: palette.warningBorder,
    colorErrorText: palette.errorText,
    colorErrorBg: palette.errorBackground,
    colorErrorBorder: palette.errorBorder,
    colorInfoText: palette.infoText,
    colorInfoBg: palette.infoBackground,
    colorInfoBorder: palette.infoBorder,
    controlOutline: palette.focus,
    borderRadius: shape.controlRadius,
    borderRadiusLG: shape.cardRadius,
    fontFamily: view.theme!.fonts.sans,
    fontFamilyCode: view.theme!.fonts.mono,
    // Compact by default: desktop-tool density for the Console too.
    fontSize: Math.round((view.density === "compact" ? 13 : 14) * view.textScale),
    controlHeight: view.density === "compact" ? 30 : 34,
    controlHeightSM: view.density === "compact" ? 24 : 28,
    controlHeightLG: view.density === "compact" ? 36 : 40,
    lineHeight: 1.5,
  };
}
