// A page's frame: a title, an optional line under it, actions on the right,
// and the page's own content below. Inside Settings it is a section heading.
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { Menu as MenuIcon } from "lucide-react";
import { t } from "../i18n/en";
import { useApp } from "../lib/app-context";
import { IconButton } from "../components/ui";
import { InSettings } from "./SettingsFrame";

/** Inside another page's tab (an agent's Instructions, say): only the content
 *  and its actions, under the host page's own heading. */
export const BarePage = createContext(false);

export function PageFrame({ title, description, actions, wide, children }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
  children: ReactNode;
}) {
  const app = useApp();
  const embedded = useContext(InSettings);
  const bare = useContext(BarePage);
  useEffect(() => {
    if (!embedded) document.title = `${title} · ${app.brandName}`;
  }, [title, app.brandName, embedded]);
  if (bare)
    return (
      <>
        {actions && <div className="mb-4 flex flex-wrap items-center justify-end gap-2">{actions}</div>}
        {children}
      </>
    );
  if (embedded)
    return (
      <>
        <div className="hz-set-head flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
        </div>
        {children}
      </>
    );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-12 flex-none items-center gap-2 border-b border-line px-2 sm:px-4 lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={20} aria-hidden />
        </IconButton>
        <span className="text-[14px] font-semibold">{title}</span>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className={`mx-auto w-full ${wide ? "max-w-[1040px]" : "max-w-[760px]"} px-4 pb-16 pt-8 sm:px-8 sm:pt-10`}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="m-0 text-[20px] font-semibold tracking-tight text-ink">{title}</h1>
              {description && <p className="m-0 mt-1.5 max-w-[60ch] text-[13px] leading-relaxed text-mute">{description}</p>}
            </div>
            {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
          </div>
          <div className="mt-6">{children}</div>
        </div>
      </div>
    </div>
  );
}
