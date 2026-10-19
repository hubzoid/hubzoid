import { useEffect, useState, type ReactNode } from "react";
import { Alert, App, Button, Drawer, Grid, Layout, Menu, Segmented, Typography } from "antd";
import { Activity, Box, CalendarClock, Gauge, History, ListChecks, Menu as MenuIcon, Monitor, Moon, MoonStar, Plug, Settings, Sparkle, Sun, Users } from "lucide-react";
import { usesSignInLinks, type Me, type MyData } from "../api";
import { href } from "../hooks/useRoute";
import type { Mode } from "../lib/theme";
import { usePresentation } from "../lib/presentation";
import wordmarkLight from "../assets/brand/wordmark-light.png";
import wordmarkDark from "../assets/brand/wordmark-dark.png";

const { Text } = Typography;

export type Area = "home" | "agents" | "runs" | "work" | "sandboxes" | "budgets" | "people" | "activity" | "schedules" | "connections" | "settings" | "diagnostics";

// The portal has no login of its own — it trusts the chat app's session cookie
// (verified server-side). Signing in and out is the chat app's. The session
// cookie is HttpOnly, so it can only be cleared by the chat app's own signout
// endpoint (same origin as the portal); a client-side cookie delete would not
// invalidate it. Only redirect once the endpoint confirms it cleared the
// session — a failed or unreachable call must NOT look like a successful logout.
function SignOutButton({ me }: { me: Me }) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  async function onClick() {
    setBusy(true);
    try {
      // The Hubzoid web app (the default mode; /me says so) owns the session:
      // end it there, then show sign-in.
      if (me.web_app === true || usesSignInLinks(me)) {
        const res = await fetch("/api/auth/logout", {
          method: "POST",
          credentials: "same-origin",
          headers: { accept: "application/json" },
        });
        if (!res.ok) throw new Error(`signout failed (${res.status})`);
        location.href = "/auth";
        return;
      }
      const res = await fetch("/api/v1/auths/signout", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) throw new Error(`signout failed (${res.status})`);
      const data = await res.json().catch(() => ({}));
      try {
        localStorage.removeItem("token"); // the chat-app SPA's own copy, if present
      } catch {
        /* private mode */
      }
      const redirect =
        data && typeof data.redirect_url === "string" && data.redirect_url
          ? data.redirect_url
          : "/";
      location.href = redirect;
    } catch {
      setBusy(false);
      message.error("Couldn’t sign out. Check your connection and try again — you are still signed in.");
    }
  }
  return (
    <Button type="link" size="small" className="signout" loading={busy} onClick={onClick}>
      Sign out
    </Button>
  );
}

const items = [
  { key: "agents", icon: <Sparkle size={18} />, label: <a href={href("/agents")}>Agents</a> },
  { key: "work", icon: <ListChecks size={18} />, label: <a href={href("/work")}>Work items</a> },
  { key: "sandboxes", icon: <Box size={18} />, label: <a href={href("/sandboxes")}>Sandboxes</a> },
  { key: "budgets", icon: <Gauge size={18} />, label: <a href={href("/budgets")}>Usage limits</a> },
  { key: "people", icon: <Users size={18} />, label: <a href={href("/people")}>People</a> },
  { key: "activity", icon: <History size={18} />, label: <a href={href("/activity")}>Activity</a> },
];
// Deployment-wide pages: settings for organization administrators, diagnostics
// for anyone who manages an agent (the server checks each call).
const orgItems = [
  { key: "settings", icon: <Settings size={18} />, label: <a href={href("/settings")}>Settings</a> },
];
const diagnosticsItem = { key: "diagnostics", icon: <Activity size={18} />, label: <a href={href("/diagnostics")}>Diagnostics</a> };
// Connectors live in each agent (its Connectors tab), not in the menu. An
// admin's own schedules and connections are under "You", as on everyone's My
// workspace; connections are the web app's Settings page there.
function youItems(me: Me) {
  const connections = me.web_app ? "/account/connections" : href("/connections");
  return [
    { key: "schedules", icon: <CalendarClock size={18} />, label: <a href={href("/schedules")}>Schedules</a> },
    { key: "connections", icon: <Plug size={18} />, label: <a href={connections}>Connections</a> },
  ];
}
function navItems(me: Me) {
  return me.org_admin ? [...items, diagnosticsItem, ...orgItems] : [...items, diagnosticsItem];
}

type ActiveBrand = { name: string; logo_light?: string; logo_dark?: string } | null;
let brandCache: Promise<ActiveBrand> | null = null;

/** The deployment's activated brand (GET /api/branding), shared by every
 *  Wordmark on the page. Without one the Console keeps the Hubzoid mark. */
function useActiveBrand(): ActiveBrand {
  const [brand, setBrand] = useState<ActiveBrand>(null);
  useEffect(() => {
    brandCache ??= fetch("/api/branding", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((b) =>
        b?.manifest
          ? { name: b.manifest.identity.name, logo_light: b.manifest.assets.logo_light, logo_dark: b.manifest.assets.logo_dark }
          : null,
      )
      .catch(() => null);
    let live = true;
    const show = () =>
      void brandCache!.then((b) => {
        if (!live) return;
        setBrand(b);
        document.title = b ? `${b.name} Console` : "Hubzoid Admin Console";
      });
    show();
    // An activation from Settings → Appearance refreshes the Console's own brand.
    const onChange = () => {
      brandCache = null;
      brandCache = fetch("/api/branding", { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((b) => (b?.manifest ? { name: b.manifest.identity.name, logo_light: b.manifest.assets.logo_light, logo_dark: b.manifest.assets.logo_dark } : null))
        .catch(() => null);
      show();
    };
    window.addEventListener("hz:brand-changed", onChange);
    return () => {
      live = false;
      window.removeEventListener("hz:brand-changed", onChange);
    };
  }, []);
  return brand;
}

function Wordmark({ isDark, compact, to = "/home" }: { isDark: boolean; compact?: boolean; to?: string }) {
  const brand = useActiveBrand();
  if (brand) {
    const logo = isDark ? brand.logo_dark || brand.logo_light : brand.logo_light;
    return (
      <a className={`brand${compact ? " compact" : ""}`} href={href(to)} aria-label={brand.name} data-brand-slot="brand.logoFrame"
        style={{ display: "flex", alignItems: "center", gap: 10, color: "inherit", textDecoration: "none" }}>
        {logo && <img src={logo} alt="" style={{ width: 28, height: 28, borderRadius: 6, objectFit: "contain" }} />}
        <span style={{ fontWeight: 600, fontSize: 16 }}>{brand.name}</span>
      </a>
    );
  }
  return (
    <a className={`brand${compact ? " compact" : ""}`} href={href(to)} aria-label="Hubzoid">
      <img src={isDark ? wordmarkDark : wordmarkLight} alt="Hubzoid" className="brand-wordmark" />
    </a>
  );
}

function ThemeToggle({ mode, setMode }: { mode: Mode; setMode: (m: Mode) => void }) {
  // The deployment's allowed appearances and lock apply here as in the chat.
  const view = usePresentation();
  if (view.locked.mode) return <Text type="secondary" className="theme-toggle">Theme managed by your administrator</Text>;
  const all = [
    { value: "light" as Mode, icon: <Sun size={14} />, title: "Light", label: <span className="sr-only">Light</span> },
    { value: "dark" as Mode, icon: <Moon size={14} />, title: "Dark", label: <span className="sr-only">Dark</span> },
    { value: "oled" as Mode, icon: <MoonStar size={14} />, title: "Black (OLED)", label: <span className="sr-only">Black (OLED)</span> },
    { value: "system" as Mode, icon: <Monitor size={14} />, title: "Match system", label: <span className="sr-only">Match system</span> },
  ];
  return (
    <Segmented<Mode>
      size="small"
      value={mode}
      onChange={setMode}
      className="theme-toggle"
      aria-label="Color theme"
      options={all.filter((o) => view.allowed.modes.includes(o.value))}
    />
  );
}

function Sidebar({
  me,
  area,
  isDark,
  mode,
  setMode,
  onNavigate,
}: {
  me: Me;
  area: Area;
  isDark: boolean;
  mode: Mode;
  setMode: (m: Mode) => void;
  onNavigate?: () => void;
}) {
  return (
    <>
      <Wordmark isDark={isDark} />
      <div className="eyebrow sidebar-label">Workspace</div>
      <Menu mode="inline" selectedKeys={[area]} items={navItems(me)} onClick={onNavigate} />
      <div className="eyebrow sidebar-label">You</div>
      <Menu mode="inline" selectedKeys={[area]} items={youItems(me)} onClick={onNavigate} />
      <div className="sidebar-footer">
        <ThemeToggle mode={mode} setMode={setMode} />
        <a href="/">Open chat ↗</a>
        <div className="account">
          <Text type="secondary" className="identity">
            {me.subject}
          </Text>
          <Text type="secondary">
            {me.org_admin ? "Administrator" : "Agent administrator"}
          </Text>
          <SignOutButton me={me} />
        </div>
      </div>
    </>
  );
}

export function Shell({
  me,
  area,
  isDark,
  mode,
  setMode,
  children,
}: {
  me: Me;
  area: Area;
  isDark: boolean;
  mode: Mode;
  setMode: (m: Mode) => void;
  children: ReactNode;
}) {
  const screens = Grid.useBreakpoint();
  const compact = screens.lg === false;
  const [open, setOpen] = useState(false);
  return (
    <Layout className="shell">
      {compact ? (
        <Layout.Header className="topbar">
          <Button
            type="text"
            aria-label="Open navigation"
            icon={<MenuIcon size={20} />}
            onClick={() => setOpen(true)}
          />
          <Wordmark isDark={isDark} compact />
          <Drawer
            placement="left"
            open={open}
            onClose={() => setOpen(false)}
            size={280}
            title="Administration"
            className="nav-drawer"
          >
            <nav className="sidebar" aria-label="Administration">
              <Sidebar
                me={me}
                area={area}
                isDark={isDark}
                mode={mode}
                setMode={setMode}
                onNavigate={() => setOpen(false)}
              />
            </nav>
          </Drawer>
        </Layout.Header>
      ) : (
        <Layout.Sider width={232} className="sider">
          <nav className="sidebar" aria-label="Administration">
            <Sidebar me={me} area={area} isDark={isDark} mode={mode} setMode={setMode} />
          </nav>
        </Layout.Sider>
      )}
      <Layout.Content className="content">
        <div className="main">{children}</div>
      </Layout.Content>
    </Layout>
  );
}

/** The frame for someone who isn't an admin: their schedules and reports,
 *  their connections and the agents they can use. No administration menu. */
export function PersonShell({
  my,
  page,
  isDark,
  mode,
  setMode,
  children,
}: {
  my: MyData;
  page: "schedules" | "connections" | "agents" | "work";
  isDark: boolean;
  mode: Mode;
  setMode: (m: Mode) => void;
  children: ReactNode;
}) {
  const me: Me = { subject: my.subject, org_admin: false, manageable: [], web_app: my.web_app };
  const tabs = [
    { key: "schedules", label: "Schedules & reports", to: href("/schedules") },
    ...(my.connections ? [{ key: "connections", label: "Connections",
      to: my.web_app ? "/account/connections" : href("/connections") }] : []),
    { key: "agents", label: "Agents", to: href("/agents") },
    { key: "work", label: "Work items", to: href("/work") },
  ];
  return (
    <Layout className="shell person-shell">
      <Layout.Header className="topbar person-top">
        <div className="person-top-left">
          <Wordmark isDark={isDark} compact to="/schedules" />
          <span className="person-title">My workspace</span>
        </div>
        <div className="person-top-right">
          <a href="/">Back to chat</a>
          <Text type="secondary" className="identity">{my.subject}</Text>
          <ThemeToggle mode={mode} setMode={setMode} />
          <SignOutButton me={me} />
        </div>
      </Layout.Header>
      <nav className="person-tabs" aria-label="Your pages">
        {tabs.map((t) => (
          <a key={t.key} href={t.to} aria-current={t.key === page ? "page" : undefined}>{t.label}</a>
        ))}
      </nav>
      <Layout.Content className="content">
        <div className="main">
          {my.blocked && <Alert className="notice" type="warning" showIcon
            message="Your access is blocked. Contact your administrator." />}
          {children}
        </div>
      </Layout.Content>
    </Layout>
  );
}
