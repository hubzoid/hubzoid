import { Suspense, useContext } from "react";
import { Breadcrumb, Spin } from "antd";
import { MessageSquare } from "lucide-react";
import { type Hub, type Me } from "../api";
import { agentHref, href } from "../hooks/useRoute";
import { lazyChunk } from "../lib/lazyChunk";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar } from "../app/components/ui";
import { BarePage } from "../app/pages/barePage";
import { AccessEditor } from "./access/AccessEditor";
import { RunsScreen } from "./RunsScreen";
import { ActivityScreen } from "./ActivityScreen";
import { EvalsScreen } from "./EvalsScreen";
import { AgentConnectors } from "./ConnectorsScreen";
import { AgentSecrets } from "./SecretsScreen";
import { AgentChannels } from "./ChannelsScreen";
import { AgentIntake } from "./IntakeScreen";
import { AgentFeedback } from "./FeedbackScreen";
import { AgentSetup } from "./SetupScreen";
import { WorkItemsScreen } from "./WorkItemsScreen";
import { agentName } from "../lib/agentName";
import { SandboxesScreen } from "./SandboxesScreen";

const WorkspacePage = lazyChunk(() => import("../app/pages/WorkspacePage"));
const HubChangesPage = lazyChunk(() => import("../app/pages/HubChangesPage"));

export type AgentTab =
  | "access" | "connectors" | "instructions" | "changes" | "runs" | "work" | "sandboxes"
  | "evals" | "feedback" | "setup" | "secrets" | "channels" | "intake" | "activity";

/** The agent page's sections: a few tabs, each holding its related parts as
 *  sub-tabs (a nested view), instead of one tab per screen. */
const SECTIONS: { key: AgentTab; label: string; parts?: [AgentTab, string][] }[] = [
  { key: "access", label: "Access" },
  { key: "connectors", label: "Connectors" },
  { key: "instructions", label: "Instructions", parts: [["instructions", "Files"], ["changes", "Changes to review"]] },
  { key: "runs", label: "Runs & schedules", parts: [["runs", "Workflows"], ["work", "Work items"], ["sandboxes", "Sandboxes"]] },
  { key: "evals", label: "Quality", parts: [["evals", "Evals"], ["feedback", "Feedback"]] },
  { key: "setup", label: "Setup", parts: [["setup", "Settings"], ["secrets", "Secrets"], ["channels", "Channels"], ["intake", "Intake"]] },
  { key: "activity", label: "Activity" },
];
const sectionOf = (tab: AgentTab) => SECTIONS.find((s) => s.key === tab || s.parts?.some(([k]) => k === tab)) ?? SECTIONS[0];

export function AgentDetail({
  hub,
  hubs,
  me,
  tab,
  rest,
  embedded = false,
}: {
  hub: Hub;
  hubs: Hub[];
  me: Me;
  tab: AgentTab;
  /** Remaining route segments after the tab (workflow and run id, or an eval run). */
  rest: string[];
  /** Inside the chat app's Settings. */
  embedded?: boolean;
}) {
  const app = useContext(AppContext);
  const agent = app?.agents.list.find((a) => a.hub === hub.key);
  const name = agentName(agent?.name, hub.name, hub.key);
  const section = sectionOf(tab);
  const chat = hub.can_chat && agent ? `/new/${encodeURIComponent(agent.id)}` : null;
  return (
    <>
      <Breadcrumb items={[{ title: <a href={href("/agents")}>Agents</a> }, { title: name }]} />
      <div className="hz-agent-head">
        <AgentAvatar name={name} src={agent?.avatar_url} size={44} />
        <div className="min-w-0 flex-1">
          <h1>{name}</h1>
          {agent?.description && <p>{agent.description}</p>}
        </div>
        {chat && (
          <a className="hz-btn hz-btn-secondary" href={chat}>
            <MessageSquare size={15} aria-hidden /> Open chat
          </a>
        )}
      </div>
      <nav className="hz-agent-tabs" aria-label="Agent sections">
        {SECTIONS.map((s) => (
          <a key={s.key} href={agentHref(hub.key, s.key)} aria-current={section.key === s.key ? "page" : undefined}>{s.label}</a>
        ))}
      </nav>
      {section.parts && (
        <nav className="hz-subtabs" aria-label={section.label}>
          {section.parts.map(([key, label]) => (
            <a key={key} href={agentHref(hub.key, key)} aria-current={tab === key ? "page" : undefined}>{label}</a>
          ))}
        </nav>
      )}
      <div key={`${hub.key}:${tab}`}>
        {tab === "access" && <AccessEditor hub={hub} embedded={embedded} />}
        {tab === "connectors" && <AgentConnectors hub={hub} me={me} embedded={embedded} />}
        {tab === "instructions" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><WorkspacePage agent={hub.key} /></Suspense>
          </BarePage.Provider>
        )}
        {tab === "changes" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><HubChangesPage hub={hub.key} /></Suspense>
          </BarePage.Provider>
        )}
        {tab === "runs" && <RunsScreen hub={hub} workflow={rest[0]} run={rest[1]} embedded={embedded} />}
        {tab === "work" && <WorkItemsScreen hubs={[hub]} selected={rest[0] ? { hub: hub.key, item: rest[0] } : undefined} />}
        {tab === "sandboxes" && <SandboxesScreen hubs={[hub]} />}
        {tab === "evals" && <EvalsScreen hub={hub} run={rest[0]} embedded={embedded} />}
        {tab === "setup" && <AgentSetup hub={hub} />}
        {tab === "secrets" && <AgentSecrets hub={hub} />}
        {tab === "channels" && <AgentChannels hub={hub} rest={rest} />}
        {tab === "intake" && <AgentIntake hub={hub} rest={rest} />}
        {tab === "feedback" && <AgentFeedback hub={hub} />}
        {tab === "activity" && <ActivityScreen hubs={hubs} hub={hub} />}
      </div>
    </>
  );
}
