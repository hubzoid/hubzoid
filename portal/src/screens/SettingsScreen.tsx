// Console → Settings: deployment settings, each owned by its domain and
// changed through that domain's own validate, diff and apply. Chat
// (chat/config.yaml), Appearance (branding.yaml) and Admin (admin.yaml).
import { lazy, Suspense } from "react";
import { Spin, Tabs, Typography } from "antd";
import { href } from "../hooks/useRoute";

const ChatSettings = lazy(() => import("./settings/ChatSettings").then((m) => ({ default: m.ChatSettings })));
const Appearance = lazy(() => import("./settings/Appearance").then((m) => ({ default: m.Appearance })));
const Parity = lazy(() => import("./settings/Parity").then((m) => ({ default: m.Parity })));
const AdminSettings = lazy(() => import("./settings/AdminSettings").then((m) => ({ default: m.AdminSettings })));

const TABS = [
  { key: "chat", label: "Chat", node: <ChatSettings /> },
  { key: "appearance", label: "Appearance", node: <Appearance /> },
  { key: "admin", label: "Admin", node: <AdminSettings /> },
  { key: "parity", label: "Parity ledger", node: <Parity /> },
];

export function SettingsScreen({ tab }: { tab?: string }) {
  const active = TABS.some((t) => t.key === tab) ? tab! : "chat";
  return (
    <div className="screen">
      <Typography.Title level={2} style={{ marginTop: 0 }}>
        Settings
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        Deployment settings. Each tab shows the file it comes from, what is in effect and where each value comes from.
        The CLI and API change the same settings with the same checks.
      </Typography.Paragraph>
      <Tabs
        activeKey={active}
        onChange={(k) => (location.hash = href(`/settings/${k}`).slice(1))}
        items={TABS.map((t) => ({
          key: t.key,
          label: t.label,
          children: <Suspense fallback={<Spin />}>{t.key === active ? t.node : null}</Suspense>,
        }))}
      />
    </div>
  );
}
