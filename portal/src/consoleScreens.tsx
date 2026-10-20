// The Console's screens by hash route, shared by the standalone Console and the
// chat app's Settings page (which shows the same screens without the Console's
// own navigation).
import { lazyChunk } from "./lib/lazyChunk";
import { type ReactNode } from "react";
import type { Hub, Me } from "./api";
import { RecoveryScreen } from "./components/common";
import type { AgentTab } from "./screens/AgentDetail";

// Each screen is its own chunk so the first paint only needs the shell.
const HomeScreen = lazyChunk(() => import("./screens/HomeScreen").then((m) => ({ default: m.HomeScreen })));
const AgentDetail = lazyChunk(() => import("./screens/AgentDetail").then((m) => ({ default: m.AgentDetail })));
const PeopleScreen = lazyChunk(() => import("./screens/PeopleScreen").then((m) => ({ default: m.PeopleScreen })));
const ActivityScreen = lazyChunk(() => import("./screens/ActivityScreen").then((m) => ({ default: m.ActivityScreen })));
const AllRunsScreen = lazyChunk(() => import("./screens/AllRunsScreen").then((m) => ({ default: m.AllRunsScreen })));
const ConfirmScreen = lazyChunk(() => import("./screens/ConfirmScreen").then((m) => ({ default: m.ConfirmScreen })));
export const SchedulesScreen = lazyChunk(() => import("./screens/SchedulesScreen").then((m) => ({ default: m.SchedulesScreen })));
export const ConnectionsScreen = lazyChunk(() => import("./screens/ConnectionsScreen").then((m) => ({ default: m.ConnectionsScreen })));
const SettingsScreen = lazyChunk(() => import("./screens/SettingsScreen").then((m) => ({ default: m.SettingsScreen })));
export const WorkItemsScreen = lazyChunk(() => import("./screens/WorkItemsScreen").then((m) => ({ default: m.WorkItemsScreen })));
const SandboxesScreen = lazyChunk(() => import("./screens/SandboxesScreen").then((m) => ({ default: m.SandboxesScreen })));
const UsageScreen = lazyChunk(() => import("./screens/UsageScreen").then((m) => ({ default: m.UsageScreen })));
const DiagnosticsScreen = lazyChunk(() => import("./screens/DiagnosticsScreen").then((m) => ({ default: m.DiagnosticsScreen })));

const TABS: AgentTab[] = ["overview", "access", "connectors", "instructions", "changes", "runs", "work", "sandboxes", "evals", "feedback", "setup", "secrets", "channels", "intake", "activity"];

/** `embedded`: inside the chat app's Settings page, which has its own navigation. */
/** Which part of the management screens a route belongs to. */
export type Area = "home" | "agents" | "runs" | "work" | "sandboxes" | "budgets" | "people" | "activity" | "schedules" | "connections" | "settings" | "diagnostics";

export function consoleScreen(parts: string[], me: Me, list: Hub[], reloadHubs: () => void, embedded = false): { screen: ReactNode; active: Area } {
  const [area, ...rest] = parts;
  let screen: ReactNode;
  let active: Area = "agents";
  if (!area || area === "home") {
    screen = <HomeScreen hubs={list} reloadHubs={reloadHubs} embedded={embedded} />;
  } else if (area === "agents") {
    if (!rest[0]) screen = <HomeScreen hubs={list} reloadHubs={reloadHubs} embedded={embedded} />;
    else {
      const hub = list.find((h) => h.key === rest[0]);
      const tab = (rest[1] || "overview") as AgentTab;
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
            subtitle={`${hub.name} has Overview, Access, Connectors, Instructions, Runs & schedules, Quality, Setup and Activity, but no “${tab}”.`}
            to={`/agents/${encodeURIComponent(hub.key)}/overview`}
            label={`Open ${hub.name}`}
          />
        );
      else screen = <AgentDetail hub={hub} hubs={list} me={me} tab={tab} rest={rest.slice(2)} embedded={embedded} />;
    }
  } else if (area === "runs") {
    active = "runs";
    screen = <AllRunsScreen hubs={list} />;
  } else if (area === "work") {
    active = "work";
    screen = <WorkItemsScreen hubs={list} selected={rest[0] && rest[1] ? { hub: rest[0], item: rest[1] } : undefined} />;
  } else if (area === "sandboxes") {
    active = "sandboxes";
    screen = <SandboxesScreen hubs={list} />;
  } else if (area === "budgets" || area === "usage") {
    active = "budgets";
    screen = <UsageScreen hubs={list} />;
  } else if (area === "people") {
    active = "people";
    screen = <PeopleScreen me={me} hubs={list} selected={rest[0]} embedded={embedded} />;
  } else if (area === "activity") {
    active = "activity";
    screen = <ActivityScreen hubs={list} />;
  } else if (area === "schedules") {
    active = "schedules";
    screen = <SchedulesScreen admin hubs={list} embedded={embedded} />;
  } else if (area === "connections") {
    active = "connections";
    screen = <ConnectionsScreen />;
  } else if (area === "settings") {
    active = "settings";
    screen = <SettingsScreen tab={rest[0]} embedded={embedded} />;
  } else if (area === "diagnostics") {
    active = "diagnostics";
    screen = <DiagnosticsScreen hubs={list} embedded={embedded} />;
  } else if (area === "confirm" && rest[0]) {
    active = "people";
    screen = <ConfirmScreen id={rest[0]} hubs={list} />;
  } else if (area === "connectors") {
    // Connectors moved into each agent; an old bookmark lands here.
    screen = (
      <RecoveryScreen
        title="Connectors are in each agent"
        subtitle="Open an agent, then its Connectors tab, to add or change the servers people connect to there."
      />
    );
  } else {
    screen = (
      <RecoveryScreen
        title="Page not found"
        subtitle="That link doesn’t point at anything here. Start from your agents."
      />
    );
  }

  return { screen, active };
}
