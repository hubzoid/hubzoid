// One Settings page for everything personal and, for people who manage the
// deployment, everything about the team: a section list on the left and the
// section on the right, inside the chat shell. Personal sections are the chat
// app's own (Sources among them); team sections show the management screens
// (agents, people, activity, schedules, chat defaults, brand, health) without
// their own navigation, and Agent instructions for administrators. The URL
// hash names the section, so links and Back work.
import { lazyChunk } from "../../lib/lazyChunk";
import { Suspense, useEffect, useState, type ReactNode } from "react";
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
  Gauge,
  LogOut,
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
import { Button, Notice, PageSpinner, cx } from "../components/ui";
import { get } from "../lib/api";
import { displayName } from "../lib/format";
import { toast } from "../components/toast";
import { AppearanceForm, DataForm, NotificationsForm, PasswordForm, ProfileForm } from "./AccountPage";
import { InSettings, Section } from "./SettingsFrame";
import { PageErrorBoundary } from "../components/ErrorBoundary";
import { MyAccess } from "../components/MyAccess";

const ConnectionsPage = lazyChunk(() => import("./ConnectionsPage"));
const SourcesPage = lazyChunk(() => import("./SourcesPage"));
const WorkspacePage = lazyChunk(() => import("./WorkspacePage"));
const SecurityPage = lazyChunk(() => import("./SecurityPage"));
const SignInSettings = lazyChunk(() => import("./SignInSettings"));
const ConsoleEmbed = lazyChunk(() => import("../../ConsoleEmbed"));
const WorkflowAccessPage = lazyChunk(() => import("./WorkflowAccessPage"));
const HubChangesPage = lazyChunk(() => import("./HubChangesPage"));
const MemoryPage = lazyChunk(() => import("./MemoryPage"));

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
  | "budgets"
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
  { key: "budgets", label: "Usage", icon: <Gauge size={16} aria-hidden />, hash: "/usage", team: true, keywords: "budget tokens cost limits usage spending" },
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

type Tab = { key: Key; label: string; also?: Key[]; sub?: { key: Key; label: string }[] };
type Group = { id: string; label: string; lede: string; icon: ReactNode; members: Key[]; tabs?: Tab[]; team?: boolean; big?: boolean };

/** The Settings nav: four big destinations for running the team's agents, then
 *  a quieter personal group. Old section links still open inside their group. */
const GROUPS: Group[] = [
  { id: "agents", label: S.agents, lede: "", icon: <Bot size={18} aria-hidden />, big: true, team: true,
    members: ["agents", "work", "sandboxes", "hub-changes", "instructions"] },
  { id: "schedules", label: S.schedules, lede: "", icon: <CalendarClock size={18} aria-hidden />, big: true,
    members: ["schedules", "my-schedules"] },
  { id: "usage", label: "Usage", lede: "", icon: <Gauge size={18} aria-hidden />, big: true, team: true, members: ["budgets"] },
  { id: "team", label: "Team", lede: "People, what they can do, how they sign in, and the workspace they share.",
    icon: <UsersRound size={18} aria-hidden />, big: true, team: true,
    members: ["people", "activity", "workflow-access", "signin", "chat", "brand", "health", "advanced", "parity"],
    tabs: [
      { key: "people", label: "People" },
      { key: "activity", label: "Activity" },
      { key: "workflow-access", label: "Permissions" },
      { key: "signin", label: "Sign-in" },
      { key: "chat", label: "Workspace", also: ["brand", "advanced", "parity"],
        sub: [{ key: "chat", label: "Chat defaults" }, { key: "brand", label: "Brand" }, { key: "advanced", label: "Advanced" }, { key: "parity", label: "Compatibility" }] },
      { key: "health", label: "Health" },
    ] },
  { id: "preferences", label: S.preferences, lede: "How Hubzoid looks and behaves for you, and how you sign in.",
    icon: <UserRound size={16} aria-hidden />, members: ["profile", "security", "appearance", "notifications", "data"],
    tabs: [
      { key: "profile", label: S.profile },
      { key: "security", label: S.security },
      { key: "appearance", label: S.appearance },
      { key: "notifications", label: S.notifications },
      { key: "data", label: S.data },
    ] },
  { id: "connections", label: "Connections", lede: "Accounts your agents use for you, and the sources they can cite.",
    icon: <Plug size={16} aria-hidden />, members: ["connections", "sources"],
    tabs: [{ key: "connections", label: "Accounts" }, { key: "sources", label: S.sources }] },
  { id: "memory", label: t.memory.title, lede: "", icon: <Brain size={16} aria-hidden />, members: ["memory"] },
  { id: "my-work", label: S.myWork, lede: "", icon: <ListChecks size={16} aria-hidden />, members: ["my-work"] },
];

type ManagedHub = { key: string; name: string; avatar?: string | null };

/** The agents this person manages, with the chat app's names and avatars. */
function useManagedHubs(team: boolean): ManagedHub[] {
  const app = useApp();
  const [hubs, setHubs] = useState<ManagedHub[]>([]);
  useEffect(() => {
    if (!team) return;
    let alive = true;
    get<{ hubs: { key: string; name: string }[] }>("/portal/api/hubs", { quiet401: true })
      .then((r) => alive && setHubs(r.hubs.map((h) => {
        const agent = app.agents.list.find((a) => a.hub === h.key);
        return { key: h.key, name: agent?.name && agent.name !== h.key ? agent.name : displayName(h.name) || h.key, avatar: agent?.avatar_url };
      })))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [team, app.agents.list]);
  return hubs;
}

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
  if (a === "budgets" || a === "usage") return "budgets";
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
  // Hub changes also opens for curators who may propose or review changes.
  const visible = (i: Item) => (i.key === "parity" ? app.isAdmin && !!app.user?.parity
    : i.admin ? app.isAdmin : i.key === "hub-changes" ? team || !!app.user?.hub_changes : i.team ? team : true);
  const items = ITEMS.filter(visible).filter((i) => !(team && i.key === "my-schedules"));
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

  // The nav is a few big destinations; each holds its sections as tabs (a
  // nested view) instead of one page per section.
  const groups = GROUPS.filter((g) => !g.team || team)
    .map((g) => ({ ...g, members: g.members.filter((k) => items.some((i) => i.key === k)) }))
    .filter((g) => g.members.length > 0);
  const groupOf = (key: Key | null) => (key ? groups.find((g) => g.members.includes(key)) : undefined);
  const activeGroup = groupOf(active);
  const tabs = (activeGroup?.tabs ?? [])
    .filter((tab) => items.some((i) => i.key === tab.key))
    .map((tab) => ({ ...tab, item: items.find((i) => i.key === tab.key)! }));
  const activeTab = tabs.find((tab) => tab.key === active || tab.also?.includes(active as Key));
  const subTabs = (activeTab?.sub ?? [])
    .filter((sub) => items.some((i) => i.key === sub.key))
    .map((sub) => ({ ...sub, item: items.find((i) => i.key === sub.key)! }));
  const q = query.trim().toLowerCase();
  const matches = q ? items.filter((i) => `${i.label} ${i.keywords ?? ""}`.toLowerCase().includes(q)) : [];
  const hubs = useManagedHubs(team);
  const link = (target: string, label: ReactNode, current: boolean, className = "hz-nav", icon?: ReactNode, extra?: ReactNode) => (
    <a
      href={`/settings#${target}`}
      onClick={(e) => {
        e.preventDefault();
        go(target);
      }}
      className={className}
      aria-current={current ? "page" : undefined}
    >
      {icon}
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {extra}
    </a>
  );
  const groupLink = (g: (typeof groups)[number]) => {
    const first = items.find((i) => i.key === g.members[0])!;
    const current = activeGroup?.id === g.id;
    return (
      <div key={g.id} className="hz-set-big-wrap">
        {link(first.hash, g.label, current, cx("hz-nav", g.big && "hz-set-big"), g.icon,
          g.id === "agents" && hubs.length > 0 ? <span className="hz-set-count" aria-hidden>{hubs.length}</span> : undefined)}
      </div>
    );
  };

  return (
    <InSettings.Provider value>
      <div className="flex min-h-0 flex-1 flex-col">
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
              {q ? (
                <>
                  {matches.map((i) => link(i.hash, i.label, active === i.key, "hz-nav", i.icon))}
                  {matches.length === 0 && <p className="text-mute px-2">{S.noMatches(query)}</p>}
                </>
              ) : (
                <>
                  <div className="hz-set-bigs">{groups.filter((g) => g.big).map(groupLink)}</div>
                  <div className="hz-set-group">{S.personalGroup}</div>
                  {groups.filter((g) => !g.big).map(groupLink)}
                </>
              )}
              <About />
            </nav>
            <main className={cx("hz-set-main", active === "chat" && "hz-set-chat", tabs.length > 1 && "is-grouped")} aria-label={current?.label}>
              <button type="button" className="hz-set-back" onClick={() => {
                  history.replaceState(null, "", "/settings");
                  window.dispatchEvent(new HashChangeEvent("hashchange"));
                }}>
                <ArrowLeft size={14} aria-hidden />
                {t.account.settings}
              </button>
              {activeGroup && tabs.length > 1 && (
                <div className="hz-set-grouphead">
                  <h1>{activeGroup.label}</h1>
                  <p>{activeGroup.lede}</p>
                </div>
              )}
              {tabs.length > 1 && (
                <nav className="hz-preference-tabs" aria-label={activeGroup?.label}>
                  {tabs.map((tab) => (
                    <a key={tab.key} href={`/settings#${tab.item.hash}`} aria-current={activeTab?.key === tab.key ? "page" : undefined}
                      onClick={(e) => { e.preventDefault(); go(tab.item.hash); }}>{tab.label}</a>
                  ))}
                </nav>
              )}
              {subTabs.length > 1 && (
                <nav className="hz-subtabs" aria-label={activeTab?.label}>
                  {subTabs.map((sub) => (
                    <a key={sub.key} href={`/settings#${sub.item.hash}`} aria-current={active === sub.key ? "page" : undefined}
                      onClick={(e) => { e.preventDefault(); go(sub.item.hash); }}>{sub.label}</a>
                  ))}
                </nav>
              )}
              {/* A section that fails shows its own error; another section works again. */}
              {current && <PageErrorBoundary resetKey={current.key}><SectionBody item={current} /></PageErrorBoundary>}
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
