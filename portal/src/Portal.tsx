import { Suspense, useCallback } from "react";
import { Alert, App, Button, ConfigProvider, Spin, Typography } from "antd";
import { signInHref, type Hub, type Me, type MyData } from "./api";
import { useData } from "./hooks/useData";
import { useRoute } from "./hooks/useRoute";
import { PersonShell, Shell } from "./components/Shell";
import { consoleScreen, ConnectionsScreen, MyAgents, SchedulesScreen } from "./consoleScreens";
import { useThemeMode } from "./lib/theme";
import { displayName } from "./app/lib/format";
import "./portal.css";


export default function Portal() {
  const { mode, setMode, isDark, config } = useThemeMode();
  return (
    <ConfigProvider theme={config}>
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
  // One portal for everyone, asked in parallel: /me answers for an admin (the
  // full Console), /my for everyone else (their own pages in a frame of their
  // own). An admin's Console never waits on /my.
  const me = useData<Me>("/me");
  const hubs = useData<{ hubs: Hub[] }>("/hubs");
  const notAdmin = !!me.error && (me.status === 401 || me.status === 403);
  const my = useData<MyData>("/my");
  const loading = (
    <div className="gate" role="status" aria-label="Loading">
      <Spin />
    </div>
  );
  const confirming = route.parts[0] === "confirm";
  if (notAdmin && !my.data)
    return (
      <div className="gate">
        <Typography.Title level={1} style={{ fontSize: 28 }}>Hubzoid</Typography.Title>
        {my.error ? (
          <Alert
            type="error"
            showIcon
            title={my.status === 401 ? "Sign in to continue" : "Couldn’t connect"}
            description={
              <>
                {my.status === 401
                  ? confirming && "Sign in to review this change. If you land in the chat app afterwards, open the confirmation link again."
                  : "Check your connection and try again."}
                <div style={{ marginTop: 12 }}>
                  {my.status === 401 ? (
                    <Button type="primary" href={signInHref()}>Sign in</Button>
                  ) : (
                    <Button onClick={my.reload}>Try again</Button>
                  )}
                </div>
              </>
            }
          />
        ) : (
          <Spin />
        )}
      </div>
    );
  // With the web app, everything here lives in its Settings page.
  if (me.data?.web_app === true) {
    const hash = location.hash && location.hash !== "#/" && location.hash !== "#/home" ? location.hash : "";
    const area = route.parts[0];
    location.replace(area === "connections" ? "/settings#/me/connections" : `/settings${hash || "#/agents"}`);
    return loading;
  }
  if (notAdmin && my.data?.web_app === true) {
    location.replace(route.parts[0] === "connections" ? "/settings#/me/connections" : "/settings#/me/schedules");
    return loading;
  }
  if (notAdmin && my.data) {
    const area = route.parts[0];
    const page = area === "connections" ? "connections" : area === "agents" ? "agents" : "schedules";
    return (
      <PersonShell my={my.data} page={page} mode={mode} setMode={setMode} isDark={isDark}>
        <Suspense fallback={loading}>
          {page === "connections" ? <ConnectionsScreen /> : page === "agents" ? <MyAgents agents={my.data.agents} /> : <SchedulesScreen admin={false} />}
        </Suspense>
      </PersonShell>
    );
  }

  const status = me.status ?? hubs.status;
  if (!me.data || !hubs.data)
    return (
      <div className="gate">
        <Typography.Title level={1} style={{ fontSize: 28 }}>Hubzoid Admin Console</Typography.Title>
        {me.error || hubs.error ? (
          <Alert
            type="error"
            showIcon
            title={status === 401 ? "Sign in to continue" : status === 403 ? "Console access is not enabled for this account" : "Couldn’t connect to the Console"}
            description={
              <>
                {status === 401
                  ? confirming &&
                    "Sign in to review this change. If you land in the chat app afterwards, open the confirmation link again."
                  : status === 403 ? "Ask your hub owner for Manage access. You can still open chat to use the agents available to you." : "Check your connection and try again. Your access has not changed."}
                <div style={{ marginTop: 12 }}>
                  {/* Signed out: one way on, back to this page. */}
                  {status === 401 ? (
                    <Button type="primary" href={signInHref()}>
                      Sign in
                    </Button>
                  ) : (
                    <>
                      <Button onClick={() => { me.reload(); hubs.reload(); }}>Try again</Button>{" "}
                      <Button type="primary" href="/">
                        Go to the chat app
                      </Button>
                    </>
                  )}
                </div>
              </>
            }
          />
        ) : (
          <Spin />
        )}
      </div>
    );

  const list = hubs.data.hubs.map((h) => ({ ...h, name: displayName(h.name) || h.key }));
  const { screen, active } = consoleScreen(route.parts, me.data, list, hubs.reload);
  return (
    <Shell me={me.data} area={active} mode={mode} setMode={setMode} isDark={isDark}>
      <Suspense fallback={loading}>{screen}</Suspense>
    </Shell>
  );
}
