import { useEffect, type ReactNode } from "react";
import { Menu as MenuIcon } from "lucide-react";
import { t } from "../i18n/en";
import { useApp } from "../lib/app-context";
import { linkClick } from "../lib/router";
import { IconButton, cx } from "../components/ui";

const tabs = [
  { href: "/account", label: t.account.account },
  { href: "/account/connections", label: t.account.connections },
];

/** Account and Connections share one frame with tabs between them. */
export function SettingsFrame({ title, current, children }: { title: string; current: string; children: ReactNode }) {
  const app = useApp();
  useEffect(() => {
    document.title = `${title} · ${app.brandName}`;
  }, [title, app.brandName]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-14 flex-none items-center gap-2 border-b border-line px-2 sm:px-4 lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={20} aria-hidden />
        </IconButton>
        <span className="text-[15px] font-semibold">{title}</span>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[720px] px-4 pb-16 pt-8 sm:px-8 sm:pt-12">
          <h1 className="m-0 text-[26px] font-semibold tracking-tight text-ink">{title}</h1>
          <nav aria-label={t.account.account} className="mt-5 flex gap-1 border-b border-line">
            {tabs.map((tab) => {
              const active = tab.href === current;
              return (
                <a
                  key={tab.href}
                  href={tab.href}
                  onClick={(e) => linkClick(e, tab.href)}
                  aria-current={active ? "page" : undefined}
                  className={cx(
                    "-mb-px border-b-2 px-3 pb-2.5 pt-1 text-[14px] font-medium no-underline",
                    active ? "border-accent text-ink" : "border-transparent text-mute hover:text-ink",
                  )}
                >
                  {tab.label}
                </a>
              );
            })}
          </nav>
          <div className="mt-8">{children}</div>
        </div>
      </div>
    </div>
  );
}

export function Section({
  title,
  description,
  children,
  id,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  const headingId = id ?? `sec-${title.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <section aria-labelledby={headingId} className="border-b border-line-soft py-7 first:pt-0 last:border-b-0">
      <div className="grid gap-5 sm:grid-cols-[200px_1fr] sm:gap-8">
        <div>
          <h2 id={headingId} className="m-0 text-[15px] font-semibold text-ink">
            {title}
          </h2>
          {description && <p className="m-0 mt-1 text-[13.5px] leading-relaxed text-mute">{description}</p>}
        </div>
        <div className="min-w-0">{children}</div>
      </div>
    </section>
  );
}
