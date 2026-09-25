import { Suspense, lazy, useCallback, type ReactNode } from "react";
import { Alert, App, Button, ConfigProvider, Spin, Typography } from "antd";
import type { Hub, Me } from "./api";
import { useData } from "./hooks/useData";
import { useRoute } from "./hooks/useRoute";
import { Shell, type Area } from "./components/Shell";
import { RecoveryScreen } from "./components/common";
import type { AgentTab } from "./screens/AgentDetail";
import { lightTheme, darkTheme, useThemeMode } from "./lib/theme";
import "./portal.css";

// Each screen is its own chunk so the first paint only needs the shell.
const HomeScreen = lazy(() => import("./screens/HomeScreen").then((m) => ({ default: m.HomeScreen })));
const AgentsScreen = lazy(() => import("./screens/AgentsScreen").then((m) => ({ default: m.AgentsScreen })));
const AgentDetail = lazy(() => import("./screens/AgentDetail").then((m) => ({ default: m.AgentDetail })));
const PeopleScreen = lazy(() => import("./screens/PeopleScreen").then((m) => ({ default: m.PeopleScreen })));
const ActivityScreen = lazy(() => import("./screens/ActivityScreen").then((m) => ({ default: m.ActivityScreen })));
const AllRunsScreen = lazy(() => import("./screens/AllRunsScreen").then((m) => ({ default: m.AllRunsScreen })));

const TABS: AgentTab[] = ["access", "runs", "activity"];

export default function Portal() {
  const { mode, setMode, isDark } = useThemeMode();
  return (
    <ConfigProvider theme={isDark ? darkTheme : lightTheme}>
      <App>
        <Router mode={mode} setMode={setMode} isDark={isDark} />
      </App>
    </ConfigProvider>
  );
}

function Router({
  mode,
  setMode,
  isDark,
}: {
  mode: import("./lib/theme").Mode;
  setMode: (m: import("./lib/theme").Mode) => void;
  isDark: boolean;
}) {
  const { modal, message } = App.useApp();
  const onBlocked = useCallback(
    (blocked: { reason: "busy" | "dirty"; proceed: () => void }) => {
      if (blocked.reason === "busy") {
        message.warning("Wait for the current save to finish before leaving.");
        return;
      }
      modal.confirm({
        title: "Leave without saving?",
        content: "You have access changes that haven’t been saved. Leaving discards them.",
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

  if (!me.data || !hubs.data)
    return (
      <div className="gate">
        <Typography.Title level={3}>Hubzoid administration</Typography.Title>
        {me.error || hubs.error ? (
          <Alert
            type="error"
            showIcon
            title={me.error || hubs.error}
            description={
              <>
                Sign in to the chat app with an account that is allowed to manage agent access, then come back here.
                <div style={{ marginTop: 12 }}>
                  <Button type="primary" href="/">
                    Go to the chat app
                  </Button>
                </div>
              </>
            }
          />
        ) : (
          <Spin />
        )}
      </div>
    );

  const [area, ...rest] = route.parts;
  const list = hubs.data.hubs;
  let screen: ReactNode;
  let active: Area = "agents";
  if (!area || area === "home") {
    active = "home";
    screen = <HomeScreen />;
  } else if (area === "agents") {
    if (!rest[0]) screen = <AgentsScreen me={me.data} hubs={list} />;
    else {
      const hub = list.find((h) => h.key === rest[0]);
      const tab = (rest[1] || "access") as AgentTab;
      if (!hub)
        screen = (
          <RecoveryScreen
            title="Agent not found"
            subtitle={`No agent “${rest[0]}” is registered here, or you don’t manage it. Pick one from the list.`}
          />
        );
      else if (!TABS.includes(tab))
        screen = (
          <RecoveryScreen
            title="Page not found"
            subtitle={`${hub.name} has Access, Runs & schedules and Activity, but no “${tab}”.`}
            to={`/agents/${encodeURIComponent(hub.key)}/access`}
            label={`Open ${hub.name}`}
          />
        );
      else screen = <AgentDetail hub={hub} hubs={list} tab={tab} rest={rest.slice(2)} />;
    }
  } else if (area === "runs") {
    active = "runs";
    screen = <AllRunsScreen hubs={list} />;
  } else if (area === "people") {
    active = "people";
    screen = <PeopleScreen me={me.data} hubs={list} selected={rest[0]} />;
  } else if (area === "activity") {
    active = "activity";
    screen = <ActivityScreen hubs={list} />;
  } else {
    screen = (
      <RecoveryScreen
        title="Page not found"
        subtitle="That link doesn’t point at anything here. Start from your agents."
      />
    );
  }

  return (
    <Shell me={me.data} area={active} mode={mode} setMode={setMode} isDark={isDark}>
      <Suspense
        fallback={
          <div className="gate" role="status" aria-label="Loading">
            <Spin />
          </div>
        }
      >
        {screen}
      </Suspense>
    </Shell>
  );
}
