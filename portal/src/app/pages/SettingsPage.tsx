// One Settings page for everything personal and, for people who manage the
// deployment, everything about the team: a section list on the left and the
// section on the right, inside the chat shell. Personal sections are the chat
// app's own (Sources among them); team sections show the management screens
// (agents, people, activity, schedules, chat defaults, brand, health) without
// their own navigation, and Agent instructions for administrators. The URL
// hash names the section, so links and Back work.
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowLeft,
  Bell,
  BookOpen,
  Bot,
  Brain,
  CalendarClock,
  Database,
  FilePen,
  HeartPulse,
  Box,
  LogOut,
  Menu as MenuIcon,
  MessageSquare,
  Palette,
  PencilRuler,
  Plug,
  ListChecks,
  Search,
  SlidersHorizontal,
  ShieldCheck,
  Sparkles,
  Star,
  UserRound,
  UsersRound,
  X,
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
import { MyAccess } from "../components/MyAccess";

const ConnectionsPage = lazy(() => import("./ConnectionsPage"));
const SourcesPage = lazy(() => import("./SourcesPage"));
const WorkspacePage = lazy(() => import("./WorkspacePage"));
const SecurityPage = lazy(() => import("./SecurityPage"));
const SignInSettings = lazy(() => import("./SignInSettings"));
const ConsoleEmbed = lazy(() => import("../../ConsoleEmbed"));
const WorkflowAccessPage = lazy(() => import("./WorkflowAccessPage"));
const HubChangesPage = lazy(() => import("./HubChangesPage"));
const MemoryPage = lazy(() => import("./MemoryPage"));

type Key =
  | "profile"
  | "security"
  | "appearance"
  | "notifications"
  | "connections"
  | "sources"
  | "data"
  | "my-schedules"
  | "my-work"
  | "work"
  | "sandboxes"
  | "agents"
  | "people"
  | "activity"
  | "schedules"
  | "chat"
  | "signin"
  | "instructions"
  | "brand"
  | "health"
  | "advanced"
  | "parity"
  | "workflow-access"
  | "hub-changes"
  | "memory";

/** `team`: for people who manage the deployment; `admin`: administrators only
 *  (the chat app's own section rather than a management screen). */
type Item = { key: Key; label: string; icon: ReactNode; hash: string; team?: boolean; admin?: boolean; keywords?: string };

const S = t.settingsPage;
const ITEMS: Item[] = [
  { key: "profile", label: S.profile, icon: <UserRound size={16} aria-hidden />, hash: "/me/profile", keywords: "name password sign out email" },
  { key: "security", label: S.security, icon: <ShieldCheck size={16} aria-hidden />, hash: "/me/security", keywords: "password two-step mfa authenticator devices sessions api keys sign out" },
  { key: "appearance", label: S.appearance, icon: <Palette size={16} aria-hidden />, hash: "/me/appearance", keywords: "theme dark light text size spacing time zone" },
  { key: "notifications", label: S.notifications, icon: <Bell size={16} aria-hidden />, hash: "/me/notifications", keywords: "alerts sound desktop" },
  { key: "connections", label: S.connections, icon: <Plug size={16} aria-hidden />, hash: "/me/connections", keywords: "accounts github google sign in" },
  { key: "sources", label: S.sources, icon: <BookOpen size={16} aria-hidden />, hash: "/me/sources", keywords: "collections documents knowledge files web pages citations" },
  { key: "data", label: S.data, icon: <Database size={16} aria-hidden />, hash: "/me/data", keywords: "import export archive delete" },
  { key: "memory", label: t.memory.title, icon: <Brain size={16} aria-hidden />, hash: "/me/memory", keywords: "remember notes personal forget" },
  { key: "my-schedules", label: S.schedules, icon: <CalendarClock size={16} aria-hidden />, hash: "/me/schedules", keywords: "reports timetable" },
  { key: "my-work", label: S.myWork, icon: <ListChecks size={16} aria-hidden />, hash: "/me/work", keywords: "approvals work items requests" },
  { key: "agents", label: S.agents, icon: <Bot size={16} aria-hidden />, hash: "/agents", team: true, keywords: "usage cost tokens connectors evals runs" },
  { key: "people", label: S.people, icon: <UsersRound size={16} aria-hidden />, hash: "/people", team: true, keywords: "invite access roles members" },
  { key: "activity", label: S.activity, icon: <Activity size={16} aria-hidden />, hash: "/activity", team: true, keywords: "runs usage history" },
  { key: "schedules", label: S.schedules, icon: <CalendarClock size={16} aria-hidden />, hash: "/schedules", team: true, keywords: "reports timetable" },
  { key: "work", label: S.workItems, icon: <ListChecks size={16} aria-hidden />, hash: "/work", team: true, keywords: "approvals operations stations" },
  { key: "sandboxes", label: S.sandboxes, icon: <Box size={16} aria-hidden />, hash: "/sandboxes", team: true, keywords: "docker workspace cleanup" },
  { key: "workflow-access", label: t.workflowAccess.title, icon: <ShieldCheck size={16} aria-hidden />, hash: "/workflow-access", team: true, keywords: "workflow permissions run schedule edit approve results who" },
  { key: "hub-changes", label: t.hubChanges.title, icon: <FilePen size={16} aria-hidden />, hash: "/hub-changes", team: true, keywords: "proposals review knowledge instructions agents.md remember diff commit undo" },
  { key: "chat", label: S.chat, icon: <MessageSquare size={16} aria-hidden />, hash: "/settings/chat", team: true, keywords: "defaults models web search" },
  { key: "signin", label: S.signIn, icon: <ShieldCheck size={16} aria-hidden />, hash: "/settings/sign-in", team: true, admin: true, keywords: "two-step mfa saml scim okta entra company directory single sign-on sso" },
  { key: "instructions", label: S.instructions, icon: <PencilRuler size={16} aria-hidden />, hash: "/workspace", team: true, admin: true, keywords: "workspace agents.md skills prompts openapi edit" },
  { key: "brand", label: S.brand, icon: <Sparkles size={16} aria-hidden />, hash: "/settings/appearance", team: true, keywords: "logo colours colors name" },
  { key: "health", label: S.health, icon: <HeartPulse size={16} aria-hidden />, hash: "/diagnostics", team: true, keywords: "diagnostics support checks" },
  { key: "advanced", label: S.advanced, icon: <SlidersHorizontal size={16} aria-hidden />, hash: "/settings/admin", team: true, admin: true, keywords: "admin.yaml advanced deployment settings plan apply" },
  { key: "parity", label: S.parity, icon: <ListChecks size={16} aria-hidden />, hash: "/settings/parity", team: true, admin: true, keywords: "parity compatibility open webui librechat checklist ledger" },
];

/** Which section a hash shows. */
function keyFor(hash: string): Key | null {
  const path = hash.replace(/^#/, "").split("?")[0];
  const [a, b] = path.split("/").filter(Boolean);
  if (a === "me") return (ITEMS.find((i) => i.hash === `/me/${b}`)?.key as Key) ?? "profile";
  if (a === "agents" || a === "home" || a === "connectors") return "agents";
  if (a === "people" || a === "confirm") return "people";
  if (a === "activity" || a === "runs") return "activity";
  if (a === "schedules") return "schedules";
  if (a === "work") return "work";
  if (a === "sandboxes") return "sandboxes";
  if (a === "workflow-access") return "workflow-access";
  if (a === "hub-changes") return "hub-changes";
  if (a === "diagnostics") return "health";
  if (a === "workspace") return "instructions";
  if (a === "settings")
    return b === "appearance" ? "brand" : b === "sign-in" ? "signin" : b === "admin" ? "advanced" : b === "parity" ? "parity" : "chat";
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
  const items = ITEMS.filter((i) => (i.admin ? app.isAdmin : i.team ? team : true)).filter((i) => !(team && i.key === "my-schedules"));
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
  // An address this person has no section for (an old link, a typo) opens the
  // first section on a wide screen rather than an empty pane. The team's
  // schedules, for someone who doesn't manage the deployment, are their own.
  useEffect(() => {
    if (current || !hash) return;
    const own = active === "schedules" && items.some((i) => i.key === "my-schedules");
    if (!own && window.innerWidth < 768) return;
    history.replaceState(null, "", `/settings#${own ? "/me/schedules" : items[0].hash}`);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, current]);
  useEffect(() => {
    document.title = `${current ? current.label : t.account.settings} · ${app.brandName}`;
  }, [current, app.brandName]);

  const personalTabs = items.filter((i) => ["profile", "security", "appearance", "notifications", "data"].includes(i.key));
  const workspaceTabs = items.filter((i) => ["chat", "signin", "brand", "health", "advanced", "parity"].includes(i.key));
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
              <About />
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
  if (item.key === "instructions")
    return (
      <Suspense fallback={<PageSpinner />}>
        <WorkspacePage agent={new URLSearchParams(location.hash.split("?")[1] ?? "").get("agent")} />
      </Suspense>
    );
  if (item.key === "signin")
    return (
      <>
        <Heading title={S.signIn} lede={S.signInLede} />
        <Suspense fallback={<PageSpinner />}>
          <SignInSettings />
        </Suspense>
      </>
    );
  if (item.key === "workflow-access")
    return (
      <Suspense fallback={<PageSpinner />}>
        <WorkflowAccessPage />
      </Suspense>
    );
  if (item.key === "hub-changes")
    return (
      <Suspense fallback={<PageSpinner />}>
        <HubChangesPage />
      </Suspense>
    );
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
            </>
          )}
          <Section title={t.myAccess.title} description={t.myAccess.help}>
            <MyAccess />
          </Section>
          {!app.isLocal && (
            <Section title={t.account.session} description={t.account.sessionHelp}>
              <SignOut />
            </Section>
          )}
        </>
      );
    case "security":
      return (
        <>
          <Heading title={S.security} lede={t.security.lede} />
          <Suspense fallback={<PageSpinner />}>
            <SecurityPage />
          </Suspense>
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
    case "sources":
      return (
        <Suspense fallback={<PageSpinner />}>
          <SourcesPage />
        </Suspense>
      );
    case "data":
      return (
        <>
          <Heading title={S.data} lede={t.data.help} />
          <DataForm />
        </>
      );
    case "memory":
      return (
        <>
          <Heading title={t.memory.title} lede={t.memory.lede} />
          <Suspense fallback={<PageSpinner />}>
            <MemoryPage />
          </Suspense>
        </>
      );
    case "my-work":
      return (
        <>
          <Heading title={S.myWork} lede={S.myWorkLede} />
          <Suspense fallback={<PageSpinner />}>
            <ConsoleEmbed personal="work" />
          </Suspense>
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

const STAR_KEY = "hz.settings.hideStar";
const REPO = "https://github.com/hubzoid/hubzoid";

/** For people who manage the deployment: the Hubzoid version, and a quiet
 *  invitation to star the project. Each person can hide the star (in this
 *  browser); `hidden_features: [github_star]` in the brand hides it for all. */
function About() {
  const app = useApp();
  const version = app.user?.version;
  const off = app.branding.manifest?.presentation?.hidden_features?.includes("github_star") ?? false;
  const [hidden, setHidden] = useState(() => {
    try {
      return localStorage.getItem(STAR_KEY) === "1";
    } catch {
      return false;
    }
  });
  if (!version) return null;
  return (
    <div className="hz-set-about">
      {!off && !hidden && (
        <div className="hz-set-star">
          <a href={REPO} target="_blank" rel="noreferrer">
            <Star size={13} aria-hidden />
            {S.star}
          </a>
          <button
            type="button"
            aria-label={S.hideStar}
            title={S.hideStar}
            onClick={() => {
              setHidden(true);
              try {
                localStorage.setItem(STAR_KEY, "1");
              } catch {
                /* hidden for this visit only */
              }
            }}
          >
            <X size={12} aria-hidden />
          </button>
        </div>
      )}
      <a className="hz-set-version" href={`${REPO}/releases/tag/v${version}`} target="_blank" rel="noreferrer">
        {S.version(version)}
      </a>
    </div>
  );
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
