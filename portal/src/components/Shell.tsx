import { useState, type ReactNode } from "react";
import { Alert, App, Button, Drawer, Grid, Layout, Menu, Segmented, Typography } from "antd";
import { CalendarClock, History, Menu as MenuIcon, Monitor, Moon, Plug, Sparkle, Sun, Users } from "lucide-react";
import { usesSignInLinks, type Me, type MyData } from "../api";
import { href } from "../hooks/useRoute";
import type { Mode } from "../lib/theme";
import wordmarkLight from "../assets/brand/wordmark-light.png";
import wordmarkDark from "../assets/brand/wordmark-dark.png";

const { Text } = Typography;

export type Area = "home" | "agents" | "runs" | "people" | "activity" | "schedules" | "connections";

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
  { key: "people", icon: <Users size={18} />, label: <a href={href("/people")}>People</a> },
  { key: "activity", icon: <History size={18} />, label: <a href={href("/activity")}>Activity</a> },
];
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
function navItems(_me: Me) {
  return items;
}

function Wordmark({ isDark, compact, to = "/home" }: { isDark: boolean; compact?: boolean; to?: string }) {
  return (
    <a className={`brand${compact ? " compact" : ""}`} href={href(to)} aria-label="Hubzoid">
      <img src={isDark ? wordmarkDark : wordmarkLight} alt="Hubzoid" className="brand-wordmark" />
    </a>
  );
}

function ThemeToggle({ mode, setMode }: { mode: Mode; setMode: (m: Mode) => void }) {
  return (
    <Segmented<Mode>
      size="small"
      value={mode}
      onChange={setMode}
      className="theme-toggle"
      aria-label="Color theme"
      options={[
        { value: "light", icon: <Sun size={14} />, title: "Light", label: <span className="sr-only">Light</span> },
        { value: "dark", icon: <Moon size={14} />, title: "Dark", label: <span className="sr-only">Dark</span> },
        { value: "system", icon: <Monitor size={14} />, title: "Match system", label: <span className="sr-only">Match system</span> },
      ]}
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
  page: "schedules" | "connections" | "agents";
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
