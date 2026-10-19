// The Console's screens inside the chat app's Settings page: the same screens,
// data and checks as the standalone Console, chosen by the URL hash, without
// the Console's own navigation (Settings has its own).
import { Suspense, useCallback, useEffect } from "react";
import { Alert, App, Button, ConfigProvider, Spin, theme } from "antd";
import type { Hub, Me, MyData } from "./api";
import { useData } from "./hooks/useData";
import { hasPendingChanges, useRoute } from "./hooks/useRoute";
import { setLeaveGuard } from "./app/lib/router";
import { useThemeMode } from "./lib/theme";
import { usePresentation } from "./lib/presentation";
import { displayName } from "./app/lib/format";
import { consoleScreen, SchedulesScreen, WorkItemsScreen } from "./consoleScreens";
import "./portal.css";

export default function ConsoleEmbed({ personal }: { personal?: "schedules" | "work" }) {
  const { config: base, isDark } = useThemeMode();
  const view = usePresentation();
  // Inside Settings the screens use the chat app's quieter scale: 13px text,
  // neutral tabs, ink as the selected colour.
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  const ink = v("--hz-ink", "#ececec");
  const mute = v("--hz-mute", "#9b9b9b");
  const scale = parseFloat(css.fontSize) / 14;
  const config = {
    ...base,
    // Compact spacing should not shrink text a second time. Keep the native
    // app's type scale, including the person's text-size preference.
    algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: { ...base.token, fontSize: 13 * scale, fontSizeSM: 12 * scale, fontSizeHeading2: 19 * scale, fontSizeHeading3: 15 * scale, borderRadius: 8, controlHeight: view.density === "compact" ? 30 : 34 },
    components: {
      ...base.components,
      Tabs: { ...(base.components?.Tabs ?? {}), inkBarColor: ink, itemSelectedColor: ink, itemHoverColor: ink, itemActiveColor: ink, itemColor: mute, titleFontSize: 13 * scale },
      Breadcrumb: { ...(base.components?.Breadcrumb ?? {}), fontSize: 12 },
      Table: { ...(base.components?.Table ?? {}), cellPaddingBlockSM: 8, cellFontSize: 13 * scale },
    },
  };
  // Leaving Settings for another page asks first when a screen has unsaved changes.
  useEffect(() => {
    setLeaveGuard(() => !hasPendingChanges() || window.confirm("Leave without saving? Your changes here are discarded."));
    return () => setLeaveGuard(null);
  }, []);
  return (
    <ConfigProvider theme={config} drawer={{ classNames: { root: "hz-native-overlay" } }} modal={{ classNames: { root: "hz-native-overlay" } }}>
      <App>
        <div className="console-embed">
          {personal === "work" ? (
            <MyWork />
          ) : personal ? (
            <Suspense fallback={<Spin />}>
              <SchedulesScreen admin={false} embedded />
            </Suspense>
          ) : (
            <Embedded />
          )}
        </div>
      </App>
    </ConfigProvider>
  );
}

/** The work this person asked for, in the agents they use. */
function MyWork() {
  const my = useData<MyData>("/my");
  if (my.error) return <Alert type="error" showIcon title="Couldn’t load your work" description={my.error} />;
  if (!my.data) return <Spin />;
  return <Suspense fallback={<Spin />}><WorkItemsScreen hubs={my.data.agents} mine /></Suspense>;
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
