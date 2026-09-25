// Hubzoid brand theme for Ant Design. Tokens come from the current Studio design
// system (HubzoidStudio/tokens/tokens.json): one accent — Signal Orange
// #E5572A — Inter + JetBrains Mono, 1px borders, no shadows. Light and dark.
import { theme as antdTheme, type ThemeConfig } from "antd";
import { useEffect, useState } from "react";

const SANS =
  'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
const MONO =
  '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace';
const ACCENT = "#B5471F";

const shared = {
  colorPrimary: ACCENT,
  colorInfo: "#345E91",
  colorSuccess: "#276444",
  colorWarning: "#805500",
  colorError: "#A3322A",
  colorLink: ACCENT,
  colorLinkHover: "#B5471F",
  borderRadius: 8,
  borderRadiusLG: 14,
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
    Alert: { withDescriptionPadding: 12, withDescriptionIconSize: 16 },
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
    colorPrimary: "#E5572A",
    colorPrimaryHover: "#F26B40",
    colorPrimaryActive: "#D64C20",
    colorTextLightSolid: "#0B0B0C",
    colorLink: "#F28B66",
    colorLinkHover: "#FFA785",
    colorSuccessBg: "#183328",
    colorSuccessBorder: "#315B43",
    colorSuccessText: "#A1D8B6",
    colorWarningBg: "#352B19",
    colorWarningBorder: "#67512C",
    colorErrorBg: "#3C2023",
    colorErrorBorder: "#754049",
    colorErrorText: "#FFB4B4",
    colorInfoBg: "#1C2D43",
    colorInfoBorder: "#395477",
    colorInfoText: "#ADCCF0",
    // The dark algorithm dims the light theme's brown warning seed too far
    // for helper text. Pin the text token independently of alert backgrounds.
    colorWarningText: "#E6BF72",
    colorWarningTextHover: "#F0CC8A",
    colorWarningTextActive: "#E6BF72",
    colorText: "#FAFAF8",
    colorTextSecondary: "#B5B5BC",
    colorBorder: "#3C3C40",
    colorBorderSecondary: "#3C3C40",
    colorBgLayout: "#151516",
    colorBgContainer: "#222224",
    colorBgElevated: "#222224",
  },
  components: {
    Button: { colorPrimary: "#E5572A", colorPrimaryHover: "#F26B40", colorPrimaryActive: "#D64C20", primaryColor: "#0B0B0C" },
    Alert: { withDescriptionPadding: 12, withDescriptionIconSize: 16 },
    Table: { headerBg: "#222224" },
    Layout: { bodyBg: "#151516", siderBg: "#222224", headerBg: "#222224" },
    Card: { colorBgContainer: "#222224" },
    Menu: { itemSelectedBg: "#2A160F", itemSelectedColor: "#F28B66" },
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
