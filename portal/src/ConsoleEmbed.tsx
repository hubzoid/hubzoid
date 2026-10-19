// The Console's screens inside the chat app's Settings page: the same screens,
// data and checks as the standalone Console, chosen by the URL hash, without
// the Console's own navigation (Settings has its own).
import { Suspense, useCallback, useEffect } from "react";
import { Alert, App, Button, ConfigProvider, Spin } from "antd";
import type { Hub, Me } from "./api";
import { useData } from "./hooks/useData";
import { hasPendingChanges, useRoute } from "./hooks/useRoute";
import { setLeaveGuard } from "./app/lib/router";
import { useThemeMode } from "./lib/theme";
import { displayName } from "./app/lib/format";
import { consoleScreen, SchedulesScreen } from "./consoleScreens";
import "./portal.css";

export default function ConsoleEmbed({ personal }: { personal?: "schedules" }) {
  const { config } = useThemeMode();
  // Leaving Settings for another page asks first when a screen has unsaved changes.
  useEffect(() => {
    setLeaveGuard(() => !hasPendingChanges() || window.confirm("Leave without saving? Your changes here are discarded."));
    return () => setLeaveGuard(null);
  }, []);
  return (
    <ConfigProvider theme={config}>
      <App>
        <div className="console-embed">
          {personal ? (
            <Suspense fallback={<Spin />}>
              <SchedulesScreen admin={false} />
            </Suspense>
          ) : (
            <Embedded />
          )}
        </div>
      </App>
    </ConfigProvider>
  );
}

function Embedded() {
  const { modal, message } = App.useApp();
  const onBlocked = useCallback(
    (blocked: { reason: "busy" | "dirty"; proceed: () => void }) => {
      if (blocked.reason === "busy") {
        message.warning("Wait for the current save to finish before leaving.");
        return;
      }
      modal.confirm({
        title: "Leave without saving?",
        content: "You have changes that haven’t been saved. Leaving discards them.",
        okText: "Discard and leave",
        okButtonProps: { danger: true },
        cancelText: "Keep editing",
        onOk: blocked.proceed,
      });
    },
    [modal, message],
  );
  const route = useRoute(onBlocked);
  const me = useData<Me>("/me");
  const hubs = useData<{ hubs: Hub[] }>("/hubs");
  if (me.error || hubs.error)
    return (
      <Alert
        type="error"
        showIcon
        title="Couldn’t load this section"
        description={
          <>
            {me.status === 403 ? "Your account can’t manage this deployment." : "Check your connection and try again."}
            <div style={{ marginTop: 12 }}>
              <Button onClick={() => { me.reload(); hubs.reload(); }}>Try again</Button>
            </div>
          </>
        }
      />
    );
  if (!me.data || !hubs.data) return <Spin />;
  const list = hubs.data.hubs.map((h) => ({ ...h, name: displayName(h.name) || h.key }));
  const { screen } = consoleScreen(route.parts, me.data, list, hubs.reload, true);
  return <Suspense fallback={<Spin />}>{screen}</Suspense>;
}
