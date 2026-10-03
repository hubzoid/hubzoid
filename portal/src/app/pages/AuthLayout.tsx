import { useEffect, type ReactNode } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { t } from "../i18n/en";
import { useApp } from "../lib/app-context";
import { useTheme, type ThemeMode } from "../lib/theme";
import { BrandMark, cx } from "../components/ui";

export function ThemeSwitch({ className }: { className?: string }) {
  const { mode, setMode } = useTheme();
  const options: { value: ThemeMode; label: string; icon: ReactNode }[] = [
    { value: "light", label: t.theme.light, icon: <Sun size={14} aria-hidden /> },
    { value: "dark", label: t.theme.dark, icon: <Moon size={14} aria-hidden /> },
    { value: "system", label: t.theme.system, icon: <Monitor size={14} aria-hidden /> },
  ];
  return (
    <div role="radiogroup" aria-label={t.theme.label} className={cx("inline-flex rounded-lg border border-line bg-raised p-0.5", className)}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={mode === o.value}
          aria-label={o.label}
          title={o.label}
          onClick={() => setMode(o.value)}
          className={cx(
            "flex h-7 w-8 items-center justify-center rounded-md text-mute hover:text-ink",
            mode === o.value && "bg-sunken text-ink",
          )}
        >
          {o.icon}
        </button>
      ))}
    </div>
  );
}

/** Centered card on the canvas for sign-in and password pages. */
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  const app = useApp();
  useEffect(() => {
    document.title = `${title} · ${app.brandName}`;
  }, [title, app.brandName]);
  const logo = app.branding.logo_url;
  return (
    <div className="flex min-h-dvh flex-col bg-canvas">
      <header className="flex items-center justify-between px-5 py-4 sm:px-8">
        <span className="flex min-w-0 items-center gap-2.5">
          <BrandMark logoUrl={logo} name={app.brandName} />
        </span>
        <ThemeSwitch />
      </header>
      <main id="main" className="flex flex-1 items-start justify-center px-4 pb-16 pt-[6vh] sm:pt-[10vh]">
        <div className="w-full max-w-[400px]">{children}</div>
      </main>
      <footer className="px-5 py-5 text-center text-xs text-mute">
        {app.brandName !== t.product && (
          <span>
            <span className="text-brand">/</span>
            {t.wordmark}
          </span>
        )}
      </footer>
    </div>
  );
}
