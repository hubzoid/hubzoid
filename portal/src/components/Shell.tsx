import { useState, type ReactNode } from "react";
import { App, Button, Drawer, Grid, Layout, Menu, Segmented, Typography } from "antd";
import { Bot, History, Menu as MenuIcon, Monitor, Moon, Play, Sun, Users } from "lucide-react";
import type { Me } from "../api";
import { href } from "../hooks/useRoute";
import type { Mode } from "../lib/theme";
import wordmarkLight from "../assets/brand/wordmark-light.png";
import wordmarkDark from "../assets/brand/wordmark-dark.png";

const { Text } = Typography;

export type Area = "agents" | "runs" | "people" | "activity";

// The portal has no login of its own — it trusts the chat app's session cookie
// (verified server-side). Signing in and out is the chat app's. The session
// cookie is HttpOnly, so it can only be cleared by the chat app's own signout
// endpoint (same origin as the portal); a client-side cookie delete would not
// invalidate it. Only redirect once the endpoint confirms it cleared the
// session — a failed or unreachable call must NOT look like a successful logout.
function SignOutButton() {
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  async function onClick() {
    setBusy(true);
    try {
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
  { key: "agents", icon: <Bot size={18} />, label: <a href={href("/agents")}>Agents</a> },
  { key: "runs", icon: <Play size={18} />, label: <a href={href("/runs")}>Runs</a> },
  { key: "people", icon: <Users size={18} />, label: <a href={href("/people")}>People</a> },
  { key: "activity", icon: <History size={18} />, label: <a href={href("/activity")}>Activity</a> },
];

function Wordmark({ isDark, compact }: { isDark: boolean; compact?: boolean }) {
  return (
    <a className={`brand${compact ? " compact" : ""}`} href={href("/agents")} aria-label="Hubzoid">
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
        { value: "light", icon: <Sun size={14} />, title: "Light" },
        { value: "dark", icon: <Moon size={14} />, title: "Dark" },
        { value: "system", icon: <Monitor size={14} />, title: "Match system" },
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
      <Menu mode="inline" selectedKeys={[area]} items={items} onClick={onNavigate} />
      <div className="sidebar-footer">
        <ThemeToggle mode={mode} setMode={setMode} />
        <a href="/">Open chat ↗</a>
        {me.org_admin && <a href="/admin">Manage accounts ↗</a>}
        <div className="account">
          <Text type="secondary" className="identity">
            {me.subject}
          </Text>
          <Text type="secondary">
            {me.org_admin ? "Organization administrator" : "Agent administrator"}
          </Text>
          <SignOutButton />
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
        <main className="main">{children}</main>
      </Layout.Content>
    </Layout>
  );
}
