// Hubzoid brand theme for Ant Design. Tokens come from the locked V1 design
// system (HubzoidStudio/colors_and_type.css): one accent — Signal Orange
// #E5572A — Inter + JetBrains Mono, 1px borders, no shadows. Light and dark.
import { theme as antdTheme, type ThemeConfig } from "antd";
import { useEffect, useState } from "react";

const SANS =
  'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
const MONO =
  '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const ACCENT = "#E5572A";

const shared = {
  colorPrimary: ACCENT,
  colorInfo: ACCENT,
  colorLink: ACCENT,
  colorLinkHover: "#B5471F",
  borderRadius: 8,
  borderRadiusLG: 8,
  borderRadiusSM: 6,
  fontFamily: SANS,
  fontFamilyCode: MONO,
  fontSize: 14,
  wireframe: false,
  // No shadows in V1.
  boxShadow: "none",
  boxShadowSecondary: "none",
  boxShadowTertiary: "none",
};

export const lightTheme: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    ...shared,
    colorText: "#0B0B0C",
    colorTextSecondary: "#6B6B70",
    colorBorder: "#E7E6E2",
    colorBorderSecondary: "#EDECE8",
    colorBgLayout: "#FAFAF8",
    colorBgContainer: "#FFFFFF",
    colorBgElevated: "#FFFFFF",
  },
  components: {
    Table: { headerBg: "#FAFAF8" },
    Layout: { bodyBg: "#FAFAF8", siderBg: "#FFFFFF", headerBg: "#FFFFFF" },
    Card: { colorBgContainer: "#FFFFFF" },
    Menu: { itemSelectedBg: "#FBEDE7", itemSelectedColor: ACCENT },
  },
};

export const darkTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    ...shared,
    colorText: "#F4F4F2",
    colorTextSecondary: "#A0A0A6",
    colorBorder: "#2A2A2E",
    colorBorderSecondary: "#232327",
    colorBgLayout: "#0B0B0C",
    colorBgContainer: "#131316",
    colorBgElevated: "#1A1A1E",
  },
  components: {
    Table: { headerBg: "#17171A" },
    Layout: { bodyBg: "#0B0B0C", siderBg: "#0F0F11", headerBg: "#0F0F11" },
    Card: { colorBgContainer: "#131316" },
    Menu: { itemSelectedBg: "#2A160F", itemSelectedColor: ACCENT },
  },
};

export type Mode = "light" | "dark" | "system";
const KEY = "hz-theme";

function systemDark() {
  return (
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches
  );
}

/** Persisted light/dark/system mode. Sets `data-theme` on <html> so the CSS
 *  variables in portal.css flip too, and returns the resolved dark flag. */
export function useThemeMode(): {
  mode: Mode;
  setMode: (m: Mode) => void;
  isDark: boolean;
} {
  const [mode, setModeState] = useState<Mode>(() => {
    try {
      const v = localStorage.getItem(KEY);
      if (v === "light" || v === "dark" || v === "system") return v;
    } catch {
      /* private mode */
    }
    return "system";
  });
  const [systemIsDark, setSystemIsDark] = useState(systemDark);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSystemIsDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const isDark = mode === "system" ? systemIsDark : mode === "dark";

  useEffect(() => {
    document.documentElement.dataset.theme = isDark ? "dark" : "light";
  }, [isDark]);

  const setMode = (m: Mode) => {
    setModeState(m);
    try {
      localStorage.setItem(KEY, m);
    } catch {
      /* ignore */
    }
  };

  return { mode, setMode, isDark };
}
