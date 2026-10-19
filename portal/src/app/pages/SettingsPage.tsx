// One Settings page for everything personal and, for people who manage the
// deployment, everything about the team: a section list on the left and the
// section on the right, inside the chat shell. Personal sections are the chat
// app's own; team sections show the management screens (agents, people,
// activity, schedules, chat defaults, brand, health) without their own
// navigation. The URL hash names the section, so links and Back work.
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowLeft,
  Bell,
  Bot,
  CalendarClock,
  Database,
  HeartPulse,
  LogOut,
  Menu as MenuIcon,
  MessageSquare,
  Palette,
  Plug,
  Search,
  Sparkles,
  UserRound,
  UsersRound,
} from "lucide-react";
import { t } from "../i18n/en";
import { post } from "../lib/api";
import { clearDeviceCache } from "../lib/devices";
import { useApp } from "../lib/app-context";
import { navigate } from "../lib/router";
import { Button, IconButton, Notice, PageSpinner, cx } from "../components/ui";
import { toast } from "../components/toast";
import { AppearanceForm, DataForm, NotificationsForm, PasswordForm, ProfileForm } from "./AccountPage";
import { InSettings, Section } from "./SettingsFrame";

const ConnectionsPage = lazy(() => import("./ConnectionsPage"));
const ConsoleEmbed = lazy(() => import("../../ConsoleEmbed"));

type Key =
  | "profile"
  | "appearance"
  | "notifications"
  | "connections"
  | "data"
  | "my-schedules"
  | "agents"
  | "people"
  | "activity"
  | "schedules"
  | "chat"
  | "brand"
  | "health";

type Item = { key: Key; label: string; icon: ReactNode; hash: string; team?: boolean; keywords?: string };

const S = t.settingsPage;
const ITEMS: Item[] = [
  { key: "profile", label: S.profile, icon: <UserRound size={16} aria-hidden />, hash: "/me/profile", keywords: "name password sign out email" },
  { key: "appearance", label: S.appearance, icon: <Palette size={16} aria-hidden />, hash: "/me/appearance", keywords: "theme dark light text size spacing time zone" },
  { key: "notifications", label: S.notifications, icon: <Bell size={16} aria-hidden />, hash: "/me/notifications", keywords: "alerts sound desktop" },
  { key: "connections", label: S.connections, icon: <Plug size={16} aria-hidden />, hash: "/me/connections", keywords: "accounts github google sign in" },
  { key: "data", label: S.data, icon: <Database size={16} aria-hidden />, hash: "/me/data", keywords: "import export archive delete" },
  { key: "my-schedules", label: S.schedules, icon: <CalendarClock size={16} aria-hidden />, hash: "/me/schedules", keywords: "reports timetable" },
  { key: "agents", label: S.agents, icon: <Bot size={16} aria-hidden />, hash: "/agents", team: true, keywords: "usage cost tokens connectors evals runs" },
  { key: "people", label: S.people, icon: <UsersRound size={16} aria-hidden />, hash: "/people", team: true, keywords: "invite access roles members" },
  { key: "activity", label: S.activity, icon: <Activity size={16} aria-hidden />, hash: "/activity", team: true, keywords: "runs usage history" },
  { key: "schedules", label: S.schedules, icon: <CalendarClock size={16} aria-hidden />, hash: "/schedules", team: true, keywords: "reports timetable" },
  { key: "chat", label: S.chat, icon: <MessageSquare size={16} aria-hidden />, hash: "/settings/chat", team: true, keywords: "defaults models web search" },
  { key: "brand", label: S.brand, icon: <Sparkles size={16} aria-hidden />, hash: "/settings/appearance", team: true, keywords: "logo colours colors name" },
  { key: "health", label: S.health, icon: <HeartPulse size={16} aria-hidden />, hash: "/diagnostics", team: true, keywords: "diagnostics support checks advanced compatibility" },
];

/** Which section a hash shows. */
function keyFor(hash: string): Key | null {
  const path = hash.replace(/^#/, "").split("?")[0];
  const [a, b] = path.split("/").filter(Boolean);
  if (a === "me") return (ITEMS.find((i) => i.hash === `/me/${b}`)?.key as Key) ?? "profile";
  if (a === "agents" || a === "home") return "agents";
  if (a === "people" || a === "confirm") return "people";
  if (a === "activity" || a === "runs") return "activity";
  if (a === "schedules") return "schedules";
  if (a === "diagnostics") return "health";
  if (a === "settings") return b === "appearance" ? "brand" : b === "admin" || b === "parity" ? "health" : "chat";
  return null;
}

function useHash(): string {
  const [hash, setHash] = useState(() => location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function go(hash: string) {
  if (location.hash === `#${hash}`) return;
  location.hash = hash;
}

export default function SettingsPage({ section }: { section: string | null }) {
  const app = useApp();
  const hash = useHash();
  const team = app.isAdmin || !!app.user?.console;
  const items = ITEMS.filter((i) => (i.team ? team : true)).filter((i) => !(team && i.key === "my-schedules"));
  const [query, setQuery] = useState("");
  // ?section= (from links elsewhere in the app) becomes the hash.
  useEffect(() => {
    const fromQuery = section && items.find((i) => i.key === section);
    if (fromQuery) {
      history.replaceState(null, "", `/settings#${fromQuery.hash}`);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } else if (!keyFor(location.hash)) {
      // A narrow screen starts on the list of sections; a wide one on the first.
      if (window.innerWidth >= 768) history.replaceState(null, "", `/settings#${items[0].hash}`);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);
  const active = keyFor(hash);
  const current = items.find((i) => i.key === active) ?? null;
  useEffect(() => {
    document.title = `${current ? current.label : t.account.settings} · ${app.brandName}`;
  }, [current, app.brandName]);

  const personalTabs = items.filter((i) => ["profile", "appearance", "notifications", "data"].includes(i.key));
  const workspaceTabs = items.filter((i) => ["chat", "brand", "health"].includes(i.key));
  const tabs = personalTabs.some((i) => i.key === active) ? personalTabs : workspaceTabs.some((i) => i.key === active) ? workspaceTabs : [];
  const groupKey = (key: Key) => personalTabs.some((i) => i.key === key) ? "profile" : workspaceTabs.some((i) => i.key === key) ? "chat" : key;
  const groupLabel = (item: Item) => item.key === "profile" ? S.preferences : item.key === "chat" ? S.workspaceSettings : item.label;
  const q = query.trim().toLowerCase();
  const shown = q
    ? items.filter((i) => `${i.label} ${i.keywords ?? ""} ${groupLabel(i)}`.toLowerCase().includes(q))
    : items.filter((i) => groupKey(i.key) === i.key);
  const nav = (group: "personal" | "team") => {
    const list = shown.filter((i) => (group === "team" ? i.team : !i.team));
    if (!list.length) return null;
    return (
      <>
        <div className="hz-set-group">{group === "team" ? S.teamGroup : S.personalGroup}</div>
        {list.map((i) => (
          <a
            key={i.key}
            href={`/settings#${i.hash}`}
            onClick={(e) => {
              e.preventDefault();
              go(i.hash);
            }}
            className="hz-nav"
            aria-current={active && (q ? active === i.key : groupKey(active) === i.key) ? "page" : undefined}
          >
            {i.icon}
            <span className="truncate">{q ? i.label : groupLabel(i)}</span>
          </a>
        ))}
      </>
    );
  };

  return (
    <InSettings.Provider value>
      <div className="flex min-h-0 flex-1 flex-col">
        <header className="hz-chat-header lg:!hidden">
          <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
            <MenuIcon size={19} aria-hidden />
          </IconButton>
          <Button size="sm" variant="ghost" icon={<ArrowLeft size={14} aria-hidden />} onClick={() => navigate("/")}>
            {S.backToChat}
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto" data-testid="settings">
          <div className={cx("hz-set", !current && "is-index")}>
            <nav className="hz-set-nav" aria-label={S.sections}>
              <button type="button" className="hz-set-backlink" onClick={() => navigate("/")}>
                <ArrowLeft size={14} aria-hidden />
                {S.backToChat}
              </button>
              <h1 className="hz-set-title">{t.account.settings}</h1>
              <label className="hz-set-find">
                <Search size={14} aria-hidden className="text-mute" />
                <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={S.search} aria-label={S.search} />
              </label>
              {nav("personal")}
              {nav("team")}
              {shown.length === 0 && <p className="text-mute px-2">{S.noMatches(query)}</p>}
            </nav>
            <main className={cx("hz-set-main", active === "chat" && "hz-set-chat")} aria-label={current?.label}>
              <button type="button" className="hz-set-back" onClick={() => {
                  history.replaceState(null, "", "/settings");
                  window.dispatchEvent(new HashChangeEvent("hashchange"));
                }}>
                <ArrowLeft size={14} aria-hidden />
                {t.account.settings}
              </button>
              {tabs.length > 0 && (
                <nav className="hz-preference-tabs" aria-label={current && personalTabs.includes(current) ? S.personalPreferences : S.workspaceSettings}>
                  {tabs.map((item) => (
                    <a key={item.key} href={`/settings#${item.hash}`} aria-current={active === item.key ? "page" : undefined}
                      onClick={(e) => { e.preventDefault(); go(item.hash); }}>{item.label}</a>
                  ))}
                </nav>
              )}
              {current && <SectionBody item={current} />}
            </main>
          </div>
        </div>
      </div>
    </InSettings.Provider>
  );
}

function Heading({ title, lede }: { title: string; lede?: string }) {
  return (
    <div className="hz-set-head">
      <h2>{title}</h2>
      {lede && <p>{lede}</p>}
    </div>
  );
}

function SectionBody({ item }: { item: Item }) {
  const app = useApp();
  if (item.team)
    return (
      <Suspense fallback={<PageSpinner />}>
        <ConsoleEmbed />
      </Suspense>
    );
  switch (item.key) {
    case "profile":
      return (
        <>
          <Heading title={S.profile} lede={S.profileLede} />
          {app.isLocal ? (
            <Notice tone="info">{t.account.localNote}</Notice>
          ) : (
            <>
              <Section title={t.account.profile} description={t.account.profileHelp}>
                <ProfileForm />
              </Section>
              <Section title={t.account.password} description={t.account.passwordHelp}>
                <PasswordForm />
              </Section>
              <Section title={t.account.session} description={t.account.sessionHelp}>
                <SignOut />
              </Section>
            </>
          )}
        </>
      );
    case "appearance":
      return (
        <>
          <Heading title={S.appearance} lede={t.account.appearanceHelp} />
          <AppearanceForm />
        </>
      );
    case "notifications":
      return (
        <>
          <Heading title={S.notifications} lede={t.notices.help} />
          <NotificationsForm />
        </>
      );
    case "connections":
      return (
        <>
          <Heading title={S.connections} lede={S.connectionsLede} />
          <Suspense fallback={<PageSpinner />}>
            <ConnectionsPage connected={null} connector={null} error={null} />
          </Suspense>
        </>
      );
    case "data":
      return (
        <>
          <Heading title={S.data} lede={t.data.help} />
          <DataForm />
        </>
      );
    case "my-schedules":
      return (
        <>
          <Heading title={S.schedules} lede={S.schedulesLede} />
          <Suspense fallback={<PageSpinner />}>
            <ConsoleEmbed personal="schedules" />
          </Suspense>
        </>
      );
    default:
      return null;
  }
}

function SignOut() {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      icon={<LogOut size={14} aria-hidden />}
      loading={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await post("/api/auth/logout", undefined, { quiet401: true });
          clearDeviceCache();
          location.assign("/auth");
        } catch (err) {
          setBusy(false);
          if ((err as { status?: number })?.status === 401) location.assign("/auth");
          else toast(t.account.signOutFailed, "error");
        }
      }}
    >
      {busy ? t.account.signingOut : t.account.signOut}
    </Button>
  );
}
