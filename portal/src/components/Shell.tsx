import { useState, type ReactNode } from "react";
import { Button, Drawer, Grid, Layout, Menu, Segmented, Typography } from "antd";
import { Bot, History, Menu as MenuIcon, Monitor, Moon, Sun, Users } from "lucide-react";
import type { Me } from "../api";
import { href } from "../hooks/useRoute";
import type { Mode } from "../lib/theme";
import wordmarkLight from "../assets/brand/wordmark-light.png";
import wordmarkDark from "../assets/brand/wordmark-dark.png";

const { Text } = Typography;

export type Area = "agents" | "people" | "activity";

// The portal has no login of its own — it trusts the chat app's session cookie
// (verified server-side). Signing in and out is the chat app's. Sign out clears
// that shared session (same origin as the portal) and returns to the app, which
// then shows its sign-in.
function signOut() {
  try {
    localStorage.removeItem("token");
  } catch {
    /* private mode */
  }
  document.cookie = "token=; Max-Age=0; path=/";
  location.href = "/";
}

const items = [
  { key: "agents", icon: <Bot size={18} />, label: <a href={href("/agents")}>Agents</a> },
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
          <a className="signout" onClick={signOut} role="button" tabIndex={0}>
            Sign out
          </a>
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
